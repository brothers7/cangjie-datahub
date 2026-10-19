"""知识图谱查询服务：实体链接 / 表路由 / 约束校验 / 口径上下文（防幻觉核心）。"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import or_ as _or_
from sqlalchemy import select

from app.db.models import (EdgeType, EntityType, KgDimension, KgEdge, KgMetric, KgValidationRule,
                           MetaColumn, MetaTable, MetricStatus, RuleSeverity, RuleType)
from app.db.models.metadata import TableLayer

# 表分层路由优先级（PRD 3.6 数仓路由：优先查汇总层）
_LAYER_PRIORITY = {
    TableLayer.ADS: 4, TableLayer.DWS: 3, TableLayer.DWD: 2, TableLayer.ODS: 1, TableLayer.DIM: 0,
    TableLayer.UNKNOWN: 1,
}


@dataclass
class Candidate:
    kind: str  # metric / dimension
    id: str
    name: str
    confidence: float
    extra: dict = field(default_factory=dict)


@dataclass
class LinkedEntities:
    metrics: list[Candidate] = field(default_factory=list)
    dimensions: list[Candidate] = field(default_factory=list)


@dataclass
class TableCandidate:
    table_id: str
    table_name: str
    layer: str
    score: float
    covers_metrics: int
    covers_dims: int
    has_time_col: bool = False
    joined_dims: int = 0     # 需经 TABLE_JOIN 才能拿到的维度数（星型/雪花模型）


@dataclass
class JoinHint:
    """表间可用 JOIN 提示，注入 SQL 生成提示词。"""
    left_table: str
    right_table: str
    left_key: str
    right_key: str
    join_type: str = "left"
    inferred: bool = True
    right_comment: str = ""
    right_columns: list[dict] = field(default_factory=list)

    def to_prompt(self) -> str:
        cols = "、".join(
            f"{c.get('column_name')}" + (f"({c['comment']})" if c.get("comment") else "")
            for c in self.right_columns[:12])
        line = (f"- {self.left_table}.{self.left_key} = {self.right_table}.{self.right_key}"
                f"（{self.join_type.upper()} JOIN {self.right_table}"
                + (f"，{self.right_comment}" if self.right_comment else "") + "）")
        if cols:
            line += f"\n      {self.right_table} 可用维度列: {cols}"
        return line


@dataclass
class Violation:
    severity: str  # block / warn
    message: str
    rule: str = ""


# 维度同义词：用户口语很少说出完整维度名（"渠道" vs "渠道名称"、"区域" vs "地区名称"）。
# 作为实体链接的兜底档，只在精确/别名都没命中时使用。
DIM_TYPE_SYNONYMS: dict[str, list[str]] = {
    "geo": ["地区", "区域", "地域", "大区", "城市", "省份", "国家", "地址"],
    "channel": ["渠道", "平台", "来源", "终端", "媒介"],
    "product": ["品类", "类目", "商品", "品牌", "产品", "sku"],
    "user_segment": ["人群", "用户", "会员", "等级", "性别", "新老客"],
    "dept": ["部门", "组织", "团队", "公司"],
}
# 维度名去后缀后作为可匹配表面形式。
# 只剥离「描述性」后缀（名称/编号/ID…），不剥 状态/类型 —— 它们承载语义，
# 剥掉后「订单状态」会退化成「订单」而与「订单量」误撞。
_DIM_NAME_SUFFIX_RE = re.compile(r"(名称|编号|代码|描述|备注|ID|id)$")
# 代理键/编号类维度（渠道ID、用户ID…）：只允许精确/别名命中，
# 否则剥掉 ID 后缀后会与「渠道名称」「用户」等业务维度撞名，导致 SQL 按主键分组。
_DIM_ID_LIKE_RE = re.compile(r"(^|_)(id|uuid|guid|no|code)$|(ID|编号|代码)$")


def _is_id_like_dim(d: KgDimension) -> bool:
    return bool(_DIM_ID_LIKE_RE.search(d.name or "")
                or _DIM_ID_LIKE_RE.search(str((d.aliases or [""])[0])))


def _dim_surface_forms(d: KgDimension) -> set[str]:
    forms: set[str] = set()
    for raw in [d.name, *(d.aliases or [])]:
        if not raw:
            continue
        forms.add(raw.lower())
        stripped = _DIM_NAME_SUFFIX_RE.sub("", raw)
        if len(stripped) >= 2:
            forms.add(stripped.lower())
    return forms


# ---------- 实体链接 ----------

async def link_entities(session, question: str, datasource_id: uuid.UUID | None = None,
                        extra_metric_texts: dict[str, list] | None = None) -> LinkedEntities:
    """精确 + 别名匹配。向量召回（Qdrant）由管线补充，此处保持纯 DB 可测。"""
    q = question.lower()

    mq = select(KgMetric).where(KgMetric.status == MetricStatus.ACTIVE)
    dq = select(KgDimension).where(KgDimension.status == MetricStatus.ACTIVE)
    if datasource_id:
        mq = mq.where(KgMetric.table_id.in_(
            select(MetaTable.id).where(MetaTable.datasource_id == datasource_id)))
        # 维度经 DIM_ON_TABLE 边关联到该数据源的表。
        # 注意边方向：src=DIMENSION、dst=TABLE（见 kg/builder.py 的 _edge 调用）。
        dq = dq.where(KgDimension.id.in_(
            select(KgEdge.src_id).where(
                KgEdge.edge_type == EdgeType.DIM_ON_TABLE,
                KgEdge.src_type == EntityType.DIMENSION,
                KgEdge.dst_id.in_(
                    select(MetaTable.id).where(MetaTable.datasource_id == datasource_id)),
            )
        ))
    metrics = (await session.execute(mq)).scalars().all()
    dims = (await session.execute(dq)).scalars().all()

    result = LinkedEntities()
    for m in metrics:
        best_conf, why = 0.0, ""
        if m.name and m.name.lower() in q:
            best_conf, why = 0.95, "名称精确命中"
        else:
            # 同名指标跨表时会带上表后缀（「订单量(trade_daily)」），
            # 用户永远只会说词干，不做词干匹配的话这些指标等于不可链接。
            stem = metric_stem(m.name)
            if len(stem) >= 2 and stem.lower() in q:
                best_conf, why = 0.88, f"指标词干命中({stem})"
            for a in m.aliases or []:
                if a and a.lower() in q and len(a) >= 2:
                    best_conf, why = max(best_conf, 0.9), f"别名命中({a})"
        # 向量召回结果补充（由管线传入 {metric_id: [相似度,...]}）
        if extra_metric_texts:
            for score in extra_metric_texts.get(str(m.id), []):
                if score > best_conf:
                    best_conf, why = min(score, 0.92), "语义相似命中"
        if best_conf > 0:
            result.metrics.append(Candidate("metric", str(m.id), m.name, best_conf,
                                            {"why": why, "agg": m.agg_function,
                                             "unit": m.unit, "def": m.business_def}))

    for d in dims:
        best_conf, why = 0.0, ""
        if d.name and d.name.lower() in q:
            best_conf, why = 0.95, "名称精确命中"
        elif not _is_id_like_dim(d):
            # 非代理键维度才做表面形式/同义词兜底
            for form in _dim_surface_forms(d):
                if len(form) >= 2 and form != (d.name or "").lower() and form in q:
                    best_conf, why = 0.9, f"维度名命中({form})"
                    break
            for a in d.aliases or []:
                if a and a.lower() in q and len(a) >= 2:
                    best_conf, why = max(best_conf, 0.88), f"别名命中({a})"
            # 兜底：用户口语很少说出完整维度名（"渠道" vs "渠道名称"）
            if best_conf == 0.0:
                for syn in DIM_TYPE_SYNONYMS.get(d.dimension_type.value, []):
                    if len(syn) >= 2 and syn in q:
                        best_conf, why = 0.72, f"维度类型同义词命中({syn})"
                        break
        if best_conf > 0:
            result.dimensions.append(Candidate("dimension", str(d.id), d.name, best_conf,
                                               {"why": why, "type": d.dimension_type.value}))

    result.metrics.sort(key=lambda c: -c.confidence)
    result.dimensions.sort(key=lambda c: -c.confidence)
    return result


async def get_metric_details(session, metric_ids: list[str]) -> list[KgMetric]:
    if not metric_ids:
        return []
    rows = (await session.execute(
        select(KgMetric).where(KgMetric.id.in_([uuid.UUID(x) for x in metric_ids]))
    )).scalars().all()
    order = {mid: i for i, mid in enumerate(metric_ids)}
    return sorted(rows, key=lambda m: order.get(str(m.id), 999))


async def get_dimension_details(session, dim_ids: list[str]) -> list[KgDimension]:
    if not dim_ids:
        return []
    rows = (await session.execute(
        select(KgDimension).where(KgDimension.id.in_([uuid.UUID(x) for x in dim_ids]))
    )).scalars().all()
    order = {did: i for i, did in enumerate(dim_ids)}
    return sorted(rows, key=lambda d: order.get(str(d.id), 999))


# ---------- 表路由 ----------

async def resolve_tables(session, metric_ids: list[str], dim_ids: list[str],
                         need_time: bool = False) -> list[TableCandidate]:
    """候选表打分：分层优先级 + 指标覆盖率 + 维度可用性（PRD 3.6 数仓路由查询）。

    - need_time=True（趋势/对比类问题）：无时间列的表直接淘汰，
      否则会路由到只有单行汇总的 ads 表，趋势图与异常检测全部失效。
    - 已链接维度时：优先保留能提供这些维度的表。维度既可以在本表上（直接可用），
      也可以在 TABLE_JOIN 可达的维表上（JOIN 后可用）—— 星型/雪花模型里
      「按地区看退款金额」就属于后者，若不认这种可达性会被白名单连同路由一起拦死。
    """
    tables: dict[str, TableCandidate] = {}
    layer_by_id: dict[str, TableLayer] = {}

    async def table_of(tid) -> MetaTable | None:
        if tid is None:
            return None
        t = await session.get(MetaTable, tid)
        return t

    for m in await get_metric_details(session, metric_ids):
        t = await table_of(m.table_id)
        if t is None:
            continue
        layer_by_id[str(t.id)] = t.layer
        cand = tables.setdefault(str(t.id), TableCandidate(
            table_id=str(t.id), table_name=t.table_name, layer=t.layer.value, score=0.0,
            covers_metrics=0, covers_dims=0))
        cand.covers_metrics += 1

    # 维度可用性：维度列在本表上（直接可用）或在 TABLE_JOIN 可达的维表上（JOIN 后可用）
    dim_rows = await get_dimension_details(session, dim_ids)
    joinable = await _join_reachable_tables(session, list(tables)) if tables else {}
    for tc in tables.values():
        reach = joinable.get(tc.table_id, set())
        for d in dim_rows:
            rels = d.related_columns or []
            if any(r.get("table_id") == tc.table_id for r in rels):
                tc.covers_dims += 1
            elif any(r.get("table_id") in reach for r in rels):
                tc.joined_dims += 1

    # 时间列可用性（一次查询取全部候选表的时间列）
    if tables:
        time_tids = {str(t) for t in (await session.execute(
            select(MetaColumn.table_id).where(
                MetaColumn.table_id.in_([uuid.UUID(t) for t in tables]),
                MetaColumn.semantic_role == "time")
        )).scalars()}
        for tid, tc in tables.items():
            tc.has_time_col = tid in time_tids

    if need_time:
        with_time = {k: v for k, v in tables.items() if v.has_time_col}
        if with_time:
            tables = with_time

    if dim_rows:
        with_dim = {k: v for k, v in tables.items()
                    if v.covers_dims > 0 or v.joined_dims > 0}
        if with_dim:
            tables = with_dim

    for tc in tables.values():
        layer = layer_by_id.get(tc.table_id, TableLayer.UNKNOWN)
        # 直接可用的维度权重高于需要 JOIN 的：JOIN 成本与出错概率都更高
        tc.score = (_LAYER_PRIORITY.get(layer, 1) * 10 + tc.covers_metrics * 6
                    + tc.covers_dims * 8 + min(tc.joined_dims, 2) * 4
                    + (4 if tc.has_time_col else 0))
    return sorted(tables.values(), key=lambda x: -x.score)


async def _join_reachable_tables(session, table_ids: list[str]) -> dict[str, set[str]]:
    """{表id: 一跳 TABLE_JOIN 可达的表id 集合}（双向，另一端可以是任意表）。"""
    out: dict[str, set[str]] = {t: set() for t in table_ids}
    if not table_ids:
        return out
    ids = [uuid.UUID(t) for t in table_ids]
    # 必须用 or_：维表通常不在候选表集合里，只按 src/dst 同时命中的话一条边都查不到
    for e in (await session.execute(
            select(KgEdge).where(KgEdge.edge_type == EdgeType.TABLE_JOIN,
                                 _or_(KgEdge.src_id.in_(ids), KgEdge.dst_id.in_(ids)))
    )).scalars():
        s, d = str(e.src_id), str(e.dst_id)
        if s in out:
            out[s].add(d)
        if d in out:
            out[d].add(s)
    return out


async def get_join_hints(session, table_ids: list[str]) -> list[JoinHint]:
    """主表到维表的可用 JOIN 提示（自动推断 + 外键边），供 SQL 生成注入。

    只返回「左表在 table_ids 中、右表是 DIM 维表」的边：
    SQL 生成只需要事实表去挂维表，反向和事实表互相 JOIN 的提示没有价值。
    """
    if not table_ids:
        return []
    ids = [uuid.UUID(t) for t in table_ids]
    edges = (await session.execute(
        select(KgEdge).where(KgEdge.edge_type == EdgeType.TABLE_JOIN,
                             KgEdge.src_id.in_(ids))
    )).scalars().all()
    if not edges:
        return []
    all_ids = set(ids) | {e.dst_id for e in edges}
    table_rows = (await session.execute(
        select(MetaTable).where(MetaTable.id.in_(all_ids)))).scalars().all()
    by_id = {str(t.id): t for t in table_rows}
    cols_by_table: dict[str, list[MetaColumn]] = {}
    for c in (await session.execute(
            select(MetaColumn).where(MetaColumn.table_id.in_(all_ids)))).scalars():
        cols_by_table.setdefault(str(c.table_id), []).append(c)

    hints: list[JoinHint] = []
    for e in edges:
        left, right = by_id.get(str(e.src_id)), by_id.get(str(e.dst_id))
        if left is None or right is None or right.layer != TableLayer.DIM:
            continue
        props = e.properties or {}
        keys = props.get("join_keys") or []
        left_key = props.get("left_key") or (keys[0] if keys else "")
        right_key = props.get("right_key") or (keys[1] if len(keys) > 1 else "")
        if not left_key or not right_key:
            continue
        hints.append(JoinHint(
            left_table=left.table_name, right_table=right.table_name,
            left_key=left_key, right_key=right_key,
            join_type=props.get("join_type", "left"),
            inferred=bool(props.get("inferred", True)),
            right_comment=(right.comment or "")[:40],
            right_columns=[{"column_name": c.column_name, "comment": (c.comment or "")[:24]}
                           for c in cols_by_table.get(str(right.id), [])
                           if c.semantic_role == "dimension"],
        ))
    return hints


class _GraphCache:
    """调用级图谱预取缓存（生命周期 = 一次校验/收窄/扩张调用链，随参数传递）。

    约束校验链路里 _allowed_dim_ids 被每个指标反复调用，原先每次都全量加载
    维度表、逐指标查白名单边与 TABLE_JOIN 邻接，指标多时退化成 3×N 次全表查询。
    这里把这些只读数据在一次调用链内各查一次：
      - allowed_map：METRIC_DIM_ALLOWED 边 → {指标id: 白名单维度集合}
      - join_adj：TABLE_JOIN 边 → {表id: 一跳可达表集合}（双向）
      - active_dims / dim_by_id：维度表（ACTIVE 子集 / 全量按 id）

    刻意不做跨请求/全局缓存：图谱会被人工编辑，缓存必须随调用结束即失效，
    宁可每次调用链重新预取，也不引入脏读。
    """

    def __init__(self) -> None:
        self._allowed: dict[str, set[str]] | None = None
        self._adj: dict[str, set[str]] | None = None
        self._active_dims: list[KgDimension] | None = None
        self._dim_by_id: dict[str, KgDimension] | None = None

    async def allowed_map(self, session) -> dict[str, set[str]]:
        if self._allowed is None:
            m: dict[str, set[str]] = {}
            for src, dst in (await session.execute(
                    select(KgEdge.src_id, KgEdge.dst_id).where(
                        KgEdge.edge_type == EdgeType.METRIC_DIM_ALLOWED,
                        KgEdge.src_type == EntityType.METRIC))).all():
                m.setdefault(str(src), set()).add(str(dst))
            self._allowed = m
        return self._allowed

    async def join_adj(self, session) -> dict[str, set[str]]:
        if self._adj is None:
            adj: dict[str, set[str]] = {}
            for src, dst in (await session.execute(
                    select(KgEdge.src_id, KgEdge.dst_id).where(
                        KgEdge.edge_type == EdgeType.TABLE_JOIN))).all():
                adj.setdefault(str(src), set()).add(str(dst))
                adj.setdefault(str(dst), set()).add(str(src))
            self._adj = adj
        return self._adj

    async def active_dims(self, session) -> list[KgDimension]:
        if self._active_dims is None:
            self._active_dims = list((await session.execute(
                select(KgDimension).where(KgDimension.status == MetricStatus.ACTIVE))).scalars())
        return self._active_dims

    async def dim_by_id(self, session) -> dict[str, KgDimension]:
        """全量维度按 id（不限状态，与 get_dimension_details 同一判据）。"""
        if self._dim_by_id is None:
            self._dim_by_id = {str(d.id): d for d in (await session.execute(
                select(KgDimension))).scalars()}
        return self._dim_by_id


async def _allowed_dim_ids(session, metric, cache: _GraphCache | None = None) -> set[str] | None:
    """指标可用的维度集合。返回 None 表示未登记白名单（不做约束校验）。

    同表共现（METRIC_DIM_ALLOWED）只是白名单的一半：星型/雪花模型里
    「退款金额按地区拆分」的地区名称在 dim_region 上，与指标不同表，
    但两者可以 JOIN。若只认同表共现，所有跨表维度组合都会被拦死。
    因此这里把指标表 ``TABLE_JOIN`` 一跳可达的表上的维度也算作可用。
    """
    cache = cache or _GraphCache()
    amap = await cache.allowed_map(session)
    if str(metric.id) not in amap:
        return None
    allowed = set(amap[str(metric.id)])
    if metric.table_id is None:
        return allowed
    reach = (await cache.join_adj(session)).get(str(metric.table_id), set())
    if not reach:
        return allowed
    # 可达维表上的维度：以 related_columns 为准（与 resolve_tables 的维度可用性同一判据）
    for d in await cache.active_dims(session):
        if str(d.id) in allowed:
            continue
        if any(rel.get("table_id") in reach for rel in (d.related_columns or [])):
            allowed.add(str(d.id))
    return allowed


# ---------- 口径收窄 ----------

def metric_stem(name: str) -> str:
    """指标名去表后缀后的词干：builder 对同名跨表指标生成「订单量(trade_daily)」这类名字，
    括号前是一致的业务词干。"""
    return re.split(r"[(（]", name or "")[0].strip()


async def expand_metric_siblings(session, metrics: list[KgMetric],
                                 dims: list[KgDimension]) -> list[KgMetric]:
    """为「目标表提供不了该维度」的指标补上同词干的兄弟指标。

    同一物理列在多张表上会被建成多个指标（订单量 / 订单量(trade_daily)）。
    实体链接只按问题文本匹配，命中的那个名字最短的指标可能挂在
    ads 日汇总表上，而该表根本没有地区列；同词干的兄弟指标在 dws 上，
    JOIN 维表即可满足。不补兄弟就会一路走到「拿 ads 表查地区」的死路。
    """
    if not metrics or not dims:
        return list(metrics)
    cache = _GraphCache()
    dim_ids = {str(d.id) for d in dims}
    stems: set[str] = set()
    for m in metrics:
        if await _metric_serves_dims(session, m, dim_ids, cache):
            continue
        stems.add(metric_stem(m.name))
    if not stems:
        return list(metrics)

    out = list(metrics)
    have = {str(m.id) for m in out}
    for cand in (await session.execute(
            select(KgMetric).where(KgMetric.status == MetricStatus.ACTIVE))).scalars():
        if str(cand.id) in have or metric_stem(cand.name) not in stems:
            continue
        if not await _metric_serves_dims(session, cand, dim_ids, cache):
            continue
        out.append(cand)
        have.add(str(cand.id))
    return out


async def _metric_serves_dims(session, metric: KgMetric, dim_ids: set[str],
                              cache: _GraphCache | None = None) -> bool:
    """指标能否满足这些维度：白名单放行 ∧ 其表本身或可 JOIN 的表能给出维度列。

    dim_ids 为空（时点类问题）时只看白名单。
    """
    cache = cache or _GraphCache()
    allowed = await _allowed_dim_ids(session, metric, cache)
    if allowed is not None and not dim_ids.issubset(allowed):
        return False
    if not dim_ids or metric.table_id is None:
        return True
    reach = (await cache.join_adj(session)).get(str(metric.table_id), set()) | {str(metric.table_id)}
    by_id = await cache.dim_by_id(session)
    for did in dim_ids:
        d = by_id.get(did)
        if d is None:
            continue  # 维度已删除/不存在：与原 get_dimension_details 查不到即跳过一致
        if not any(r.get("table_id") in reach for r in (d.related_columns or [])):
            return False
    return True


async def filter_metrics_for_dims(session, metrics: list[KgMetric],
                                  dims: list[KgDimension]) -> tuple[list[KgMetric], list[KgMetric]]:
    """按已链接维度收窄指标，返回 (保留, 剔除)。

    两级判据：
      1) 白名单（METRIC_DIM_ALLOWED，含可 JOIN 到的维表）不放行 → 剔除；
      2) 白名单放行、但指标所在表既没有该维度列也 JOIN 不到 → 记为「列不可达」。

    第 2 条是否生效取决于图谱里有没有表关系（_graph_has_joins）：
      - 有表关系：不可达就是真不可达，剔除。否则会路由到一张拼不出该维度的表，
        生成的 SQL 拿不相关的表做笛卡尔积，查询"成功"但结果是错的。
      - 没有表关系：整库都不可达，属于图谱缺信息而非指标不适用，
        此时一律保留，交给约束校验给出可读报错，不静默清空候选。
    """
    if not metrics or not dims:
        return list(metrics), []
    cache = _GraphCache()
    dim_ids = {str(d.id) for d in dims}
    strict = await _graph_has_joins(session, metrics)
    kept: list[KgMetric] = []
    dropped: list[KgMetric] = []
    unreachable: list[KgMetric] = []
    for m in metrics:
        allowed = await _allowed_dim_ids(session, m, cache)
        if allowed is not None and not dim_ids.issubset(allowed):
            dropped.append(m)
        elif await _metric_serves_dims(session, m, dim_ids, cache):
            kept.append(m)
        else:
            unreachable.append(m)
    if not strict:
        kept.extend(unreachable)
    elif kept:
        dropped.extend(unreachable)
    else:
        # 一条都跑不通：返回空集，让上层给出可读的「不支持该维度组合」报错，
        # 而不是挑一条凑数的指标跑出错误数据。
        dropped.extend(unreachable)
    return (kept, dropped) if (kept or strict) else (list(metrics), [])


async def _graph_has_joins(session, metrics: list[KgMetric]) -> bool:
    """这些指标所在的库是否登记过表关系（TABLE_JOIN）。

    用来区分「指标不适用该维度」和「图谱还没建表关系」——
    后者不该拿来当收窄依据，否则刚接入的库会被收窄成空集。
    """
    ds_ids = {m.table_id for m in metrics if m.table_id}
    if not ds_ids:
        return False
    table_ids = set((await session.execute(
        select(MetaTable.id).where(MetaTable.id.in_(ds_ids))
    )).scalars())
    if not table_ids:
        return False
    ds_of = dict((await session.execute(
        select(MetaTable.id, MetaTable.datasource_id).where(MetaTable.id.in_(table_ids))
    )).all())
    scope = (await session.execute(
        select(MetaTable.id).where(MetaTable.datasource_id.in_(set(ds_of.values())))
    )).scalars().all()
    if not scope:
        return False
    return (await session.execute(
        select(KgEdge.id).where(KgEdge.edge_type == EdgeType.TABLE_JOIN,
                                _or_(KgEdge.src_id.in_(scope), KgEdge.dst_id.in_(scope)))
        .limit(1)
    )).scalar_one_or_none() is not None


# ---------- 约束校验 ----------

async def check_constraints(session, metrics: list[KgMetric],
                            dims: list[KgDimension]) -> list[Violation]:
    out: list[Violation] = []
    dim_names = [d.name for d in dims]
    dim_ids = {str(d.id) for d in dims}

    # 1) 显式禁止的维度组合（全局 + 指标级规则）
    rules = (await session.execute(
        select(KgValidationRule).where(KgValidationRule.enabled.is_(True),
                                       KgValidationRule.rule_type == RuleType.FORBIDDEN_DIM_COMBO)
    )).scalars().all()
    for r in rules:
        if r.target_type == "metric" and r.target_id and str(r.target_id) not in {str(m.id) for m in metrics}:
            continue
        combo = set(r.rule_config.get("dims") or [])
        if combo and combo.issubset(set(dim_names)):
            sev = "block" if r.severity == RuleSeverity.BLOCK else "warn"
            out.append(Violation(sev, r.message or f"维度组合 [{'/'.join(combo)}] 禁止一起使用", "forbidden_dim_combo"))

    # 2) 指标×维度 FORBIDDEN 边（一次 IN 批量查询，按指标分组，替代每指标一次查询）
    if metrics:
        forbid_by_metric: dict[str, list[str]] = {}
        for src, dst in (await session.execute(
                select(KgEdge.src_id, KgEdge.dst_id).where(
                    KgEdge.edge_type == EdgeType.METRIC_DIM_FORBIDDEN,
                    KgEdge.src_type == EntityType.METRIC,
                    KgEdge.src_id.in_([m.id for m in metrics])))).all():
            forbid_by_metric.setdefault(str(src), []).append(str(dst))
        for m in metrics:
            for dst in forbid_by_metric.get(str(m.id), []):
                if dst in dim_ids:
                    out.append(Violation("block", f"指标「{m.name}」不支持按该维度拆分（图谱约束）",
                                        "metric_dim_forbidden"))

    # 3) 维度不在指标允许集合内（存在 ALLOWED 边时白名单校验）
    #    允许集合含「可 JOIN 的维表上的维度」，见 _allowed_dim_ids
    cache = _GraphCache()
    for m in metrics:
        allowed = await _allowed_dim_ids(session, m, cache)
        if allowed is None:
            continue
        for d in dims:
            if str(d.id) not in allowed:
                out.append(Violation(
                    "block",
                    f"指标「{m.name}」不支持按「{d.name}」拆分：数据口径不支持该维度组合",
                    "dim_not_allowed"))

    # 4) 值域配置提醒（数据校验阶段复核）
    for m in metrics:
        if m.value_range and len(m.value_range) == 2:
            out.append(Violation("warn", f"指标「{m.name}」有值域约束 [{m.value_range[0]}, {m.value_range[1]}]，"
                                         "查询结果将做值域复核", "value_range_note"))

    # 5) 维度列物理不可达：白名单放行了、但表上没这列也 JOIN 不过去
    #    （白名单是人工登记的，可能与实际表结构脱节）。
    #    仅当还有别的指标能跑通时才拦；全都不可达说明是图谱缺关系，
    #    拦下来只会让人看不到任何结果，不如放行后由 SQL 阶段报错。
    if dims and len(metrics) > 1:
        for m in metrics:
            if not await _metric_serves_dims(session, m, dim_ids, cache):
                out.append(Violation(
                    "block",
                    f"指标「{m.name}」所在表既没有「{'/'.join(dim_names)}」维度列，"
                    "也无法通过表关联取得，请改用其它候选指标",
                    "dim_column_unreachable"))
    return out


# ---------- 口径上下文（注入 SQL 生成提示词） ----------

async def metric_context(session, metrics: list[KgMetric], table: MetaTable | None,
                         dims: list[KgDimension]) -> str:
    parts: list[str] = []
    parts.append("## 指标口径（必须严格遵守，禁止编造指标）")
    for m in metrics:
        line = f"- {m.name}：口径={m.business_def or '（见注释）'}"
        if m.formula:
            line += f"；公式={m.formula}"
        if m.agg_function:
            line += f"；聚合={m.agg_function}"
        if m.unit:
            line += f"；单位={m.unit}"
        aliases = "、".join(m.aliases or [])
        if aliases:
            line += f"；别名={aliases}"
        parts.append(line)

    if table is not None:
        cols = (await session.execute(
            select(MetaColumn).where(MetaColumn.table_id == table.id)
        )).scalars().all()
        parts.append(f"\n## 目标表 {table.table_name}（{table.layer.value} 层，注释：{table.comment or '无'}）")
        col_lines = [f"  {c.column_name} {c.data_type}"
                     + (f" -- {c.comment}" if c.comment else "")
                     for c in cols]
        parts.append("\n".join(col_lines[:80]))

    hints: list[JoinHint] = []
    if table is not None:
        hints = await get_join_hints(session, [str(table.id)])

    if dims:
        # 维度的物理落点优先级：主表 > 可 JOIN 的维表 > 其它表。
        # 「地区名称」同时挂在 ads_region_rank 和 dim_region 上，若不按可 JOIN 性筛选，
        # 提示词会把维度指向 ads 表，LLM 就会拿不相关的表去拼 JOIN。
        preferred = {str(table.id)} if table is not None else set()
        name_by_id = {h.right_table: h for h in hints}
        wanted = set(name_by_id)
        parts.append("\n## 维度")
        for d in dims:
            ex = "、".join(str(x) for x in (d.value_examples or [])[:10])
            line = f"- {d.name}" + (f"（可选值示例：{ex}）" if ex else "")
            # 跨表维度必须说清物理列与来源表，否则 LLM 会把维度名当列名写进 GROUP BY
            home = _dim_home_column(d, preferred, wanted)
            if home:
                line += f"；物理列 {home['table_name']}.{home['column_name']}"
                if table is not None and home.get("table_id") != str(table.id):
                    line += f"（不在主表上，需 JOIN {home['table_name']}）"
            parts.append(line)

    if hints:
        parts.append("\n## 表关联（需要跨表维度时必须用这些 JOIN 条件，禁止自行猜测关联键）")
        parts.append("\n".join(h.to_prompt() for h in hints))
    return "\n".join(parts)


def _dim_home_column(d: KgDimension, preferred_tables: set[str],
                     joinable_table_names: set[str]) -> dict | None:
    """维度的物理落点：主表优先，其次可 JOIN 的维表，最后才是任意已知落点。"""
    rels = [r for r in (d.related_columns or []) if r.get("column_name")]
    if not rels:
        return None
    for r in rels:
        if r.get("table_id") in preferred_tables:
            return r
    for r in rels:
        if r.get("table_name") in joinable_table_names:
            return r
    return rels[0]
