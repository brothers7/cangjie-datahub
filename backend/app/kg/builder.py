"""知识图谱自动构建：元数据 → 指标/维度/边 实体（PRD 3.2.4）。"""
from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import or_ as _or_
from sqlalchemy import select

from app.connectors.base import ColumnMeta, TableMeta
from app.db.models import (DimensionType, EdgeType, EntityType, KgDimension, KgEdge, KgMetric,
                          MetaColumn, MetaTable, SemanticRole, SourceKind)
from app.db.models.metadata import AggFunc, TableLayer
from app.kg import classifier as clf
from app.kg.merger import apply_auto_merge, is_fully_protected, is_tombstoned

logger = logging.getLogger(__name__)

# 常见英文字段 -> 中文指标名（演示体验优化，可扩充）
METRIC_NAME_CN = {
    "gmv": "GMV", "sales_amt": "销售额", "order_cnt": "订单量", "pay_uv": "支付UV",
    "pay_amt": "支付金额", "pay_cnt": "支付单量", "refund_amt": "退款金额",
    "refund_cnt": "退款单量", "visit_uv": "访问UV", "cart_cnt": "加购量",
    "buyer_uv": "买家数", "new_user_cnt": "新增用户数", "aov": "客单价",
    "total_amt": "总金额", "unit_price": "单价", "qty": "数量", "amount": "金额",
    "cnt": "数量", "uv": "UV", "pv": "PV", "duration": "时长", "discount_amt": "优惠金额",
}
DIM_NAME_CN = {
    "region": "地区", "channel": "渠道", "city": "城市", "province": "省份",
    "product_cate": "品类", "cate": "品类", "category": "品类", "brand": "品牌",
    "is_new": "新老客", "user_type": "用户类型", "platform": "平台", "os": "操作系统",
    "pay_type": "支付方式", "status": "状态", "gender": "性别", "level": "等级",
}

_LAYER_PREFIX_RE = re.compile(r"^(ods|dwd|dws|ads|dim|fact|aggr|summary)_")

# 外键列命名：X_id / X_key / X_code / X_no（大小写与中英混排都兼容）
_ID_LIKE_RE = re.compile(r"(^|_)(id|key|code|no)$", re.IGNORECASE)


def _strip_layer(table_name: str) -> str:
    return _LAYER_PREFIX_RE.sub("", table_name or "")


def nice_metric_name(col: ColumnMeta) -> str:
    comment = (col.comment or "").strip()
    if 1 < len(comment) <= 30 and "\n" not in comment:
        # 注释常形如 "当日GMV(元)" —— 保留
        return comment
    key = col.column_name.lower()
    if key in METRIC_NAME_CN:
        return METRIC_NAME_CN[key]
    return col.column_name


def nice_dimension_name(col: ColumnMeta) -> str:
    comment = (col.comment or "").strip()
    if 1 < len(comment) <= 20 and "\n" not in comment:
        return comment
    key = col.column_name.lower()
    if key in DIM_NAME_CN:
        return DIM_NAME_CN[key]
    return col.column_name


def default_business_def(table_comment: str, col: ColumnMeta, agg: str | None) -> str:
    tc = (table_comment or "").strip()
    cc = (col.comment or "").strip() or col.column_name
    agg_text = f"，聚合方式 {agg}" if agg else ""
    return f"【自动生成】{tc}·{cc}{agg_text}。请补充业务口径。"


class KgBuildStats:
    def __init__(self) -> None:
        self.entities_created = 0
        self.entities_updated = 0
        self.entities_skipped_manual = 0

    def to_dict(self) -> dict:
        return {"created": self.entities_created, "updated": self.entities_updated,
                "skipped_manual": self.entities_skipped_manual}


async def upsert_meta(session, ds_id, tm: TableMeta, classifications: dict,
                      stats_by_col: dict | None = None, *, layer: TableLayer | None = None) -> tuple[MetaTable, bool]:
    """写 meta_tables / meta_columns，应用人工字段保护。返回 (table, created)。"""
    stats_by_col = stats_by_col or {}
    existing = (await session.execute(
        select(MetaTable).where(
            MetaTable.datasource_id == ds_id,
            MetaTable.schema_name == tm.schema_name,
            MetaTable.table_name == tm.table_name,
        )
    )).scalar_one_or_none()

    incoming = {
        "comment": tm.comment or "",
        "layer": layer or clf.classify_table_layer(tm.table_name, tm.comment),
        "row_count": tm.row_count,
        "partition_cols": _guess_partitions(tm),
    }
    if existing is None:
        row = MetaTable(datasource_id=ds_id, schema_name=tm.schema_name,
                        table_name=tm.table_name, source=SourceKind.AUTO, **incoming)
        session.add(row)
        await session.flush()
        existing = row
        created = True
    else:
        if is_fully_protected(existing):
            return existing, False
        apply_auto_merge(existing, incoming, ["comment", "layer", "row_count", "partition_cols"])
        created = False

    # 列
    existing_cols: dict[str, MetaColumn] = {
        c.column_name: c for c in (await session.execute(
            select(MetaColumn).where(MetaColumn.table_id == existing.id)
        )).scalars()
    }
    for c in tm.columns:
        cls = classifications.get(c.column_name)
        st = stats_by_col.get(c.column_name) or {}
        col_incoming = {
            "data_type": c.data_type,
            "comment": c.comment or "",
            "is_primary_key": c.is_primary_key,
            "is_nullable": c.nullable,
            "semantic_role": cls.role if cls else SemanticRole.UNKNOWN,
            "agg_hint": cls.agg.value if cls and cls.agg else None,
            "cardinality_ratio": st.get("ratio"),
            "value_examples": (st.get("examples") or [])[:15] if cls and cls.role == SemanticRole.DIMENSION else [],
        }
        ec = existing_cols.get(c.column_name)
        if ec is None:
            session.add(MetaColumn(table_id=existing.id, column_name=c.column_name,
                                   source=SourceKind.AUTO, meta_hash="", **col_incoming))
            # 关联 KG 实体时由 build_kg 落库后回填
        else:
            if is_fully_protected(ec):
                continue
            apply_auto_merge(ec, col_incoming, list(col_incoming.keys()))
    await session.flush()
    return existing, created


def _guess_partitions(tm: TableMeta) -> list[str]:
    return [c.column_name for c in tm.columns if clf._is_time_column(c)][:1]


async def build_kg(session, ds_id: uuid.UUID, meta_table: MetaTable, tm: TableMeta,
                   classifications: dict, stats_by_col: dict | None = None,
                   stats: KgBuildStats | None = None) -> None:
    """为单表生成/更新 KG 实体与边。"""
    stats = stats or KgBuildStats()
    stats_by_col = stats_by_col or {}

    # 预取与本表相关的已存在边到内存集合（本表指标发出的边 / 指向本表的边 / 本表发出的边），
    # 后续建边差集后批量 add，替代每边一次 SELECT（指标×维度边数量级最大）。
    edge_keys = await _table_edge_keys(session, meta_table)

    # 已有列 ORM 对象
    col_rows: dict[str, MetaColumn] = {
        c.column_name: c for c in (await session.execute(
            select(MetaColumn).where(MetaColumn.table_id == meta_table.id)
        )).scalars()
    }

    # ---- 指标 ----
    for c in tm.columns:
        cls = classifications.get(c.column_name)
        if not cls or cls.role != SemanticRole.METRIC:
            continue
        col_row = col_rows.get(c.column_name)
        existing = None
        if col_row is not None:
            existing = (await session.execute(
                select(KgMetric).where(KgMetric.table_id == meta_table.id,
                                       KgMetric.column_id == col_row.id)
            )).scalar_one_or_none()
        name = nice_metric_name(c)
        st = stats_by_col.get(c.column_name) or {}
        incoming = {
            "agg_function": cls.agg.value if cls.agg else None,
            "business_def": default_business_def(tm.comment, c, cls.agg.value if cls.agg else None),
            "confidence": cls.confidence,
            "business_domain": _strip_layer(tm.table_name) or "general",
        }
        if existing is None:
            # 全局唯一名处理：同名不同物理列 → 追加表上下文
            final_name = await _unique_metric_name(session, name, meta_table)
            m = KgMetric(
                name=final_name,
                aliases=[c.column_name],
                table_id=meta_table.id,
                column_id=col_row.id if col_row else None,
                source=SourceKind.AUTO,
                period_grain="day" if meta_table.layer in (TableLayer.DWD, TableLayer.DWS, TableLayer.ADS) else "day",
                **incoming,
            )
            session.add(m)
            await session.flush()
            stats.entities_created += 1
            await _edge(session, EntityType.METRIC, m.id, EntityType.TABLE, meta_table.id,
                        EdgeType.METRIC_ON_TABLE, known=edge_keys)
        else:
            if is_fully_protected(existing) or is_tombstoned(existing):
                stats.entities_skipped_manual += 1
                continue
            # business_def 是自动生成占位，apply_auto_merge 会跳过人工改过的字段
            apply_auto_merge(existing, incoming,
                             ["agg_function", "confidence", "business_domain", "business_def"])
            stats.entities_updated += 1

    # ---- 维度 ----
    dim_ids_on_table: list[uuid.UUID] = []
    for c in tm.columns:
        cls = classifications.get(c.column_name)
        if not cls or cls.role != SemanticRole.DIMENSION:
            continue
        col_row = col_rows.get(c.column_name)
        name = nice_dimension_name(c)
        st = stats_by_col.get(c.column_name) or {}
        dim = (await session.execute(
            select(KgDimension).where(KgDimension.name == name)
        )).scalar_one_or_none()
        rel = {"table_id": str(meta_table.id), "table_name": tm.table_name,
               "column_id": str(col_row.id) if col_row else None,
               "column_name": c.column_name}
        examples = st.get("examples") or []
        if dim is None:
            dim = KgDimension(
                name=name,
                aliases=[c.column_name],
                description=f"【自动生成】来自 {tm.table_name}.{c.column_name}"
                            + (f"（{c.comment}）" if c.comment else ""),
                dimension_type=DimensionType(cls.dimension_type or "other"),
                value_examples=examples[:15],
                related_columns=[rel],
                source=SourceKind.AUTO,
            )
            session.add(dim)
            await session.flush()
            stats.entities_created += 1
        else:
            if is_fully_protected(dim) or is_tombstoned(dim):
                stats.entities_skipped_manual += 1
                dim_ids_on_table.append(dim.id)
                continue
            incoming = {
                "value_examples": (list(dim.value_examples or []) + examples)[:15],
                "related_columns": _merge_related(dim.related_columns, rel),
            }
            apply_auto_merge(dim, incoming, ["value_examples", "related_columns"])
            stats.entities_updated += 1
        dim_ids_on_table.append(dim.id)
        await _edge(session, EntityType.DIMENSION, dim.id, EntityType.TABLE, meta_table.id,
                    EdgeType.DIM_ON_TABLE, known=edge_keys)

    # ---- METRIC_DIM_ALLOWED：同表指标×维度 共现关系（约束校验的默认白名单） ----
    table_metrics = (await session.execute(
        select(KgMetric).where(KgMetric.table_id == meta_table.id)
    )).scalars().all()
    for m in table_metrics:
        if is_tombstoned(m):
            continue
        for did in dim_ids_on_table:
            await _edge(session, EntityType.METRIC, m.id, EntityType.DIMENSION, did,
                        EdgeType.METRIC_DIM_ALLOWED, known=edge_keys)

    # ---- TABLE_JOIN：库内声明了外键的优先用外键 ----
    # 命名约定推断无法覆盖的表关系由 infer_table_joins 在整库扫描结束后补齐。
    for fk in tm.foreign_keys or []:
        ref_table = (await session.execute(
            select(MetaTable).where(MetaTable.datasource_id == ds_id,
                                    MetaTable.table_name == fk["ref_table"])
        )).scalar_one_or_none()
        if ref_table is not None:
            await _edge(session, EntityType.TABLE, meta_table.id, EntityType.TABLE,
                        ref_table.id, EdgeType.TABLE_JOIN,
                        properties={"join_keys": [fk["column"], fk["ref_column"]],
                                    "left_key": fk["column"], "right_key": fk["ref_column"],
                                    "join_type": "left", "inferred": False},
                        known=edge_keys)


async def infer_table_joins(session, ds_id: uuid.UUID) -> int:
    """推断星型/雪花模型的事实表→维表关系，写入 TABLE_JOIN 边（PRD 3.6「自动抽取表关系」）。

    为什么不能只靠外键：`insp.get_foreign_keys()` 对没声明 FK 的库（SQLite 演示库、
    大量离线数仓表）一律返回空，导致 TABLE_JOIN 边全空，跨表维度组合被白名单全部拦死。
    因此在整库扫描结束后按命名约定补一轮推断：

      1) 事实表上形如 X_id / X_key / X_code 的非主键列，与维表 `dim_X` 的 X_id 主键配对；
      2) 兜底：任一表上与另一表主键列同名的列。

    只向 DIM 层建边（保守策略）：事实表之间共享 region_id 不代表业务上可以互连，
    放开会产生大量无意义 JOIN 提示，反而污染 SQL 生成提示词。
    """
    tables = (await session.execute(
        select(MetaTable).where(MetaTable.datasource_id == ds_id)
    )).scalars().all()
    if not tables:
        return 0
    cols_by_table: dict[str, list[MetaColumn]] = {}
    for c in (await session.execute(
            select(MetaColumn).where(MetaColumn.table_id.in_([t.id for t in tables])))).scalars():
        cols_by_table.setdefault(str(c.table_id), []).append(c)

    by_name: dict[str, MetaTable] = {}
    pk_col_index: dict[str, MetaTable] = {}
    for t in tables:
        if t.layer != TableLayer.DIM:
            continue
        by_name.setdefault(t.table_name.lower(), t)
        by_name.setdefault(_strip_layer(t.table_name).lower(), t)
        for x in cols_by_table.get(str(t.id), []):
            if x.is_primary_key:
                pk_col_index.setdefault((x.column_name or "").lower(), t)

    created = 0
    # 整批预取已存在的 TABLE_JOIN 边，差集后批量 add（替代每候选边一次 SELECT）
    join_keys_known = {(r[0], str(r[1]), str(r[2]), str(r[3]), str(r[4]))
                       for r in (await session.execute(
                           select(KgEdge.edge_type, KgEdge.src_type, KgEdge.src_id,
                                  KgEdge.dst_type, KgEdge.dst_id)
                           .where(KgEdge.edge_type == EdgeType.TABLE_JOIN))).all()}
    for t in tables:
        if t.layer == TableLayer.DIM:
            continue  # 维表之间不再连（雪花模型的中间维表由人工维护）
        for c in cols_by_table.get(str(t.id), []):
            col = c.column_name or ""
            if c.is_primary_key or not _ID_LIKE_RE.search(col):
                continue
            base = _strip_layer(_ID_LIKE_RE.sub("", col, count=1))
            target = by_name.get(base.lower()) if len(base) >= 2 else None
            if target is None:
                # 兜底：维表主键列名与事实表外键列名完全同名
                target = pk_col_index.get(col.lower())
            if target is None or target.id == t.id:
                continue
            pk = next((x for x in cols_by_table.get(str(target.id), []) if x.is_primary_key), None)
            if pk is None:
                continue
            if await _edge(session, EntityType.TABLE, t.id, EntityType.TABLE, target.id,
                           EdgeType.TABLE_JOIN,
                           properties={"join_keys": [col, pk.column_name],
                                       "left_key": col, "right_key": pk.column_name,
                                       "join_type": "left", "inferred": True},
                           known=join_keys_known):
                created += 1
    return created


def _merge_related(existing: list, rel: dict) -> list:
    out = list(existing or [])
    key = (rel.get("table_id"), rel.get("column_name"))
    if not any((r.get("table_id"), r.get("column_name")) == key for r in out):
        out.append(rel)
    return out


async def _unique_metric_name(session, base: str, meta_table: MetaTable) -> str:
    name = base
    while True:
        hit = (await session.execute(
            select(KgMetric).where(KgMetric.name == name)
        )).scalar_one_or_none()
        if hit is None:
            return name
        if hit.table_id == meta_table.id:
            return name  # 同表重建场景，由 column 匹配先行处理
        name = f"{base}({_strip_layer(meta_table.table_name) or meta_table.table_name})"
        base = name  # 极端碰撞时最多再叠一层
        if len(name) > 100:
            return f"{name[:96]}~"


async def _table_edge_keys(session, meta_table: MetaTable) -> set[tuple]:
    """预取与本表相关的已存在边 key 集合：本表指标发出的边（ALLOWED/METRIC_ON_TABLE）、
    指向本表的边（DIM_ON_TABLE/METRIC_ON_TABLE）、本表作为 src 的边（TABLE_JOIN/外键）。"""
    metric_ids = select(KgMetric.id).where(KgMetric.table_id == meta_table.id)
    rows = (await session.execute(
        select(KgEdge.edge_type, KgEdge.src_type, KgEdge.src_id,
               KgEdge.dst_type, KgEdge.dst_id)
        .where(_or_(
            KgEdge.src_id.in_(metric_ids),
            KgEdge.dst_id == meta_table.id,
            KgEdge.src_id == meta_table.id,
        )))).all()
    return {(r[0], str(r[1]), str(r[2]), str(r[3]), str(r[4])) for r in rows}


async def _edge(session, src_type: EntityType, src_id, dst_type: EntityType, dst_id,
                edge_type: EdgeType, properties: dict | None = None,
                known: set[tuple] | None = None) -> bool:
    """幂等建边，返回是否新建（已存在返回 False）。

    known 非空时走「预取集合 + 差集」路径：调用方一次性预取已存在边，
    本函数只做内存比对并登记新 key（防同批重复 add），消除每边一次 SELECT；
    库层唯一约束 uq_edge 仍作为并发兜底。
    """
    if known is not None:
        key = (edge_type, str(src_type), str(src_id), str(dst_type), str(dst_id))
        if key in known:
            return False
        known.add(key)
        session.add(KgEdge(src_type=src_type, src_id=src_id, dst_type=dst_type,
                           dst_id=dst_id, edge_type=edge_type,
                           properties=properties or {}, source=SourceKind.AUTO))
        await session.flush()
        return True
    hit = (await session.execute(
        select(KgEdge).where(KgEdge.edge_type == edge_type, KgEdge.src_type == src_type,
                             KgEdge.src_id == src_id, KgEdge.dst_type == dst_type,
                             KgEdge.dst_id == dst_id)
    )).scalar_one_or_none()
    if hit is None:
        session.add(KgEdge(src_type=src_type, src_id=src_id, dst_type=dst_type,
                           dst_id=dst_id, edge_type=edge_type,
                           properties=properties or {}, source=SourceKind.AUTO))
        await session.flush()
        return True
    return False
