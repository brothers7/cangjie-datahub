"""库表答疑 Skill（PRD 3.3A）：表用途 / 选表建议 / 字段释义 / 关联关系 / 使用建议 / 血缘简答。

硬约束：答案只能来自元数据（MetaTable/MetaColumn）与知识图谱（KG 指标/维度/表关系），
不执行任何查数 SQL；元数据缺失（如无注释）时必须明说「暂无注释，建议补充」，禁止编造。
"""
from __future__ import annotations

import re
import uuid

from sqlalchemy import or_ as _or_
from sqlalchemy import select

from app.agent.skills.base import skill
from app.db.models import (EdgeType, KgDimension, KgEdge, KgMetric, MetaColumn,
                           MetaTable, MetricStatus)
from app.db.models.llm import LlmScene
from app.kg import service as kg_service
from app.llm.gateway import gateway
from app.llm.types import ChatMessage

TABLE_QA_SYSTEM = """你是企业数据仓库的数据目录专家，回答用户关于库表使用的问题。
严格规则：
1. 只能依据「数据目录」中给出的表、字段、注释、指标口径与表关系作答，禁止编造任何不存在的表、字段、口径或业务含义。
2. 表或字段没有注释时，必须明说「该表/字段暂无注释，建议联系管理员补充」，不允许猜测业务含义。
3. 选表建议要给出理由（分层、覆盖的指标/维度、是否有分区），并提示优先用汇总层（DWS/ADS）而非明细层。
4. 回答关联关系时，只使用「表关系」中列出的 JOIN 键；没有登记关系时明说未登记，可给出基于同名字段的推断但必须标注「推断，未经登记」。
5. 使用建议可包括：分区裁剪、过滤条件、粒度注意事项、行数规模提示。
6. 指标口径问答（「XX 口径是什么/怎么算」）：只能引用「命中指标（口径卡）」中的业务口径与计算公式原文；口径未登记时必须明说「该指标口径未登记，请到『知识图谱』页补充」，禁止编造。
7. 指标查表引导（「XX 指标到哪张表查」）：按口径卡的「可查表」作答，多张表时优先推荐汇总层（ADS/DWS）并说明各表粒度差异；指标未命中时明说未在指标图谱中登记。
8. 简明扼要，用简体中文，必要时用短列表。"""


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^0-9a-zA-Z一-鿿]+", (text or "").lower()) if len(t) >= 2}


async def match_tables(session, datasource_id, question: str,
                       limit: int = 6) -> tuple[list[MetaTable], list[MetaTable]]:
    """定位问题涉及的表。返回 (命中表, 全量表)。

    命中路径：表名/注释关键词命中 ＋ KG 实体链接（指标/维度名 → 所在表）。
    """
    q = question.lower()
    tables = list((await session.execute(
        select(MetaTable).where(MetaTable.datasource_id == datasource_id)
        .order_by(MetaTable.layer, MetaTable.table_name)
    )).scalars())

    matched: dict[str, MetaTable] = {}
    for t in tables:
        name = (t.table_name or "").lower()
        if name and name in q:
            matched[str(t.id)] = t
            continue
        # 注释关键词命中（如「订单」「渠道」）
        if t.comment and _tokens(t.comment) & _tokens(question):
            matched.setdefault(str(t.id), t)

    # KG 链接补充：指标/维度名命中 → 挂到所在表
    try:
        linked = await kg_service.link_entities(session, question, datasource_id)
        for c in linked.metrics[:5]:
            m = await session.get(KgMetric, uuid.UUID(c.id))
            if m and m.table_id:
                t = await session.get(MetaTable, m.table_id)
                if t is not None:
                    matched.setdefault(str(t.id), t)
        for c in linked.dimensions[:5]:
            # related_columns 里的 table_id 即维度落点
            d = await session.get(KgDimension, uuid.UUID(c.id))
            for rel in (d.related_columns or []) if d else []:
                tid = rel.get("table_id")
                if tid:
                    t = await session.get(MetaTable, uuid.UUID(tid))
                    if t is not None:
                        matched.setdefault(str(t.id), t)
    except Exception:
        pass  # 链接失败不阻断答疑

    hit = list(matched.values())[:limit]
    return hit, tables


async def match_metrics(session, datasource_id, question: str,
                        limit: int = 5) -> list[KgMetric]:
    """定位问题涉及的 KG 指标：指标名/别名与问题互相包含（长度≥2 防误命中）。"""
    q = (question or "").lower()
    if not q:
        return []
    metrics = list((await session.execute(
        select(KgMetric).where(KgMetric.status == MetricStatus.ACTIVE))).scalars())
    hits: list[KgMetric] = []
    for m in metrics:
        names = [m.name, *(m.aliases or [])]
        for n in names:
            n_l = (n or "").lower()
            if len(n_l) >= 2 and (n_l in q or (len(q) >= 2 and q in n_l)):
                hits.append(m)
                break
        if len(hits) >= limit:
            break
    return hits


async def metric_cards(session, metrics: list[KgMetric]) -> list[dict]:
    """指标口径卡（结构化 payload，前端卡片渲染）：口径/公式/聚合/粒度/单位/可查表。"""
    if not metrics:
        return []
    # 可查表边一次 IN 批量取回：{指标id: [表id]}，替代每指标一次边查询
    extra_tables: dict[str, list] = {}
    for e in (await session.execute(
            select(KgEdge).where(KgEdge.edge_type == EdgeType.METRIC_ON_TABLE,
                                 KgEdge.src_id.in_([m.id for m in metrics])))).scalars():
        extra_tables.setdefault(str(e.src_id), []).append(e.dst_id)
    # 涉及表一次 IN 批量取回，替代逐表 session.get
    all_tids = {m.table_id for m in metrics if m.table_id}
    for tids in extra_tables.values():
        all_tids.update(tids)
    table_by_id: dict[str, MetaTable] = {}
    if all_tids:
        for t in (await session.execute(
                select(MetaTable).where(MetaTable.id.in_(list(all_tids))))).scalars():
            table_by_id[str(t.id)] = t

    cards: list[dict] = []
    for m in metrics:
        # 可查表：主表 + METRIC_ON_TABLE 边登记的其它表
        table_ids: list = ([m.table_id] if m.table_id else []) + extra_tables.get(str(m.id), [])
        tables: list[dict] = []
        seen: set = set()
        for tid in table_ids:
            if not tid or str(tid) in seen:
                continue
            seen.add(str(tid))
            mt = table_by_id.get(str(tid))
            if mt is not None:
                tables.append({"name": mt.table_name, "layer": mt.layer.value,
                               "comment": mt.comment or ""})
        layer_rank = {"ads": 0, "dws": 1, "dwd": 2, "dim": 3, "ods": 4}
        tables.sort(key=lambda x: layer_rank.get(x["layer"], 5))
        cards.append({
            "name": m.name, "aliases": m.aliases or [],
            "business_def": m.business_def or "", "formula": m.formula or "",
            "agg_function": m.agg_function or "", "unit": m.unit or "",
            "period_grain": m.period_grain or "", "business_domain": m.business_domain or "",
            "tables": tables,
        })
    return cards


async def build_context(session, datasource_id, question: str) -> tuple[str, list[str], list[dict]]:
    """组装数据目录上下文文本。返回 (prompt 文本, 命中表名列表, 命中指标口径卡)。"""
    hit, tables = await match_tables(session, datasource_id, question)
    metrics = await match_metrics(session, datasource_id, question)
    cards = await metric_cards(session, metrics) if metrics else []
    if not tables:
        return "", [], cards

    parts: list[str] = []
    layer_order = {"ads": 0, "dws": 1, "dwd": 2, "dim": 3, "ods": 4, "unknown": 5}

    # 命中指标（口径卡）置前：口径问答/指标查表引导的答案只能出自这里
    if cards:
        parts.append("## 命中指标（口径卡，口径问答必须引用此处原文）")
        for c in cards:
            parts.append(
                f"### 指标 {c['name']}"
                + (f"（别名：{'、'.join(c['aliases'][:4])}）" if c["aliases"] else ""))
            parts.append(f"业务口径：{c['business_def'] or '未登记'}")
            parts.append(f"计算公式：{c['formula'] or '未登记'}；聚合方式：{c['agg_function'] or '未登记'}"
                         f"；粒度：{c['period_grain'] or '未登记'}；单位：{c['unit'] or '未登记'}"
                         f"；业务域：{c['business_domain'] or 'general'}")
            if c["tables"]:
                parts.append("可查表（已按汇总层优先排序）：" + "；".join(
                    f"{t_['name']}（{t_['layer']} 层）" for t_ in c["tables"]))
            else:
                parts.append("可查表：未登记（该指标未挂接到任何表）")
        parts.append("")

    # 未命中具体表（选表类问题）：给全量表清单（一行一表）
    if not hit:
        parts.append(f"## 数据目录（共 {len(tables)} 张表，用户未指名具体表，请据此给选表建议）")
        for t in sorted(tables, key=lambda x: layer_order.get(x.layer.value, 5))[:80]:
            line = f"- {t.table_name}（{t.layer.value} 层"
            if t.row_count:
                line += f"，约 {t.row_count:,} 行"
            if t.partition_cols:
                line += f"，分区: {','.join(t.partition_cols[:3])}"
            line += f"）：{t.comment or '暂无注释'}"
            parts.append(line)
        return "\n".join(parts), [], cards

    # 命中表：给完整字段清单
    hit_ids = [t.id for t in hit]
    cols_by_table: dict[str, list[MetaColumn]] = {str(t.id): [] for t in hit}
    for c in (await session.execute(
            select(MetaColumn).where(MetaColumn.table_id.in_(hit_ids)))).scalars():
        cols_by_table.setdefault(str(c.table_id), []).append(c)

    # 命中表上的 KG 指标
    metrics_by_table: dict[str, list[KgMetric]] = {}
    for m in (await session.execute(
            select(KgMetric).where(KgMetric.table_id.in_(hit_ids),
                                   KgMetric.status == MetricStatus.ACTIVE))).scalars():
        metrics_by_table.setdefault(str(m.table_id), []).append(m)

    parts.append("## 数据目录（命中表详情）")
    for t in hit:
        head = f"### 表 {t.table_name}（{t.layer.value} 层"
        if t.row_count:
            head += f"，约 {t.row_count:,} 行"
        if t.partition_cols:
            head += f"，分区字段: {','.join(t.partition_cols[:5])}"
        head += "）"
        parts.append(head)
        parts.append(f"表注释：{t.comment or '暂无注释'}")
        mts = metrics_by_table.get(str(t.id), [])
        if mts:
            parts.append("表上指标：" + "；".join(
                f"{m.name}（口径: {m.business_def or '未登记'}）" for m in mts[:12]))
        col_lines = []
        for c in cols_by_table.get(str(t.id), [])[:60]:
            line = f"  - {c.column_name} {c.data_type}"
            role = c.semantic_role.value
            if role != "unknown":
                line += f" [{role}]"
            line += f"：{c.comment or '暂无注释'}"
            if c.value_examples:
                ex = "、".join(str(x) for x in c.value_examples[:8])
                line += f"（枚举示例: {ex}）"
            col_lines.append(line)
        parts.append("字段：\n" + ("\n".join(col_lines) if col_lines else "  （无字段信息）"))

    # 表关系（命中表作为任一端点的 JOIN 边）
    edges = (await session.execute(
        select(KgEdge).where(KgEdge.edge_type == EdgeType.TABLE_JOIN,
                             _or_(KgEdge.src_id.in_(hit_ids), KgEdge.dst_id.in_(hit_ids)))
    )).scalars().all()
    if edges:
        id_name = {str(t.id): t.table_name for t in tables}
        parts.append("\n## 表关系（已登记）")
        for e in edges[:20]:
            props = e.properties or {}
            keys = props.get("join_keys") or []
            lk = props.get("left_key") or (keys[0] if keys else "?")
            rk = props.get("right_key") or (keys[1] if len(keys) > 1 else "?")
            parts.append(
                f"- {id_name.get(str(e.src_id), '?')}.{lk} = {id_name.get(str(e.dst_id), '?')}.{rk}"
                f"（{(props.get('join_type') or 'left').upper()} JOIN"
                + ("，推断" if props.get("inferred", True) else "，人工登记") + "）")

    # 其它表清单（简版，供选表对比）
    rest = [t for t in tables if str(t.id) not in {str(h.id) for h in hit}]
    if rest:
        parts.append(f"\n## 其它表（共 {len(rest)} 张，简版清单）")
        for t in sorted(rest, key=lambda x: layer_order.get(x.layer.value, 5))[:40]:
            parts.append(f"- {t.table_name}（{t.layer.value}）：{t.comment or '暂无注释'}")

    return "\n".join(parts), [t.table_name for t in hit], cards


@skill("table_qa", "库表用途/选表/字段/关联答疑（仅基于元数据+KG）")
async def table_qa_answer(*, session, question: str, datasource_id,
                          override_config_id: str | None = None):
    """异步生成器：逐块产出答疑文本。返回 (chunks, matched_tables)——
    通过生成器产出 token，调用方负责 SSE 转发。"""
    context, matched, cards = await build_context(session, datasource_id, question)
    if not context:
        if cards:
            yield "该数据源还没有扫描元数据表，但指标图谱中命中了以下指标，口径卡如上。", [], cards
        else:
            yield "该数据源还没有元数据。请先在「数据源」页执行一次元数据扫描，再来问我库表相关的问题。", []
        return

    user = f"{context}\n\n<用户问题>{question[:500]}</用户问题>\n请基于以上数据目录回答："
    try:
        async for chunk in gateway.chat_stream(
                LlmScene.TABLE_QA,
                [ChatMessage("system", TABLE_QA_SYSTEM), ChatMessage("user", user)],
                override_config_id=override_config_id):
            if chunk.delta:
                yield chunk.delta, matched, cards
    except Exception:
        # LLM 不可用降级：直接给目录摘要（PRD 4.3 友好降级）
        yield _plain_summary(context, matched), matched, cards


def _plain_summary(context: str, matched: list[str]) -> str:
    head = "（LLM 服务暂不可用，以下为数据目录原文摘要）\n\n"
    # 只保留命中表章节，避免超长
    lines = context.split("\n")
    keep, take = [], False
    for ln in lines:
        if ln.startswith("## "):
            take = "命中表详情" in ln or "表关系" in ln
        if take:
            keep.append(ln)
    body = "\n".join(keep)[:3000]
    if matched:
        head += f"命中表：{', '.join(matched)}\n\n"
    return head + body
