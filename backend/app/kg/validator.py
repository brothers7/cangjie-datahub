"""图谱质量校验引擎（PRD 3.2.6）：重复指标 / 注释缺失 / 非法维度组合 / 缺聚合规则。"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.models import (EdgeType, EntityType, KgDimension, KgEdge, KgMetric,
                           KgValidationRule, MetaColumn, MetaTable, MetricStatus,
                           RuleSeverity, RuleType, SemanticRole, SourceKind)


async def run_validation(session, datasource_id: uuid.UUID | None = None) -> list[dict]:
    """返回问题清单：[{category, severity, entity_type, entity_id, entity_name, message}]。"""
    issues: list[dict] = []

    metrics_q = select(KgMetric).where(KgMetric.status == MetricStatus.ACTIVE)
    dims_q = select(KgDimension).where(KgDimension.status == MetricStatus.ACTIVE)
    if datasource_id:
        metrics_q = metrics_q.where(KgMetric.table_id.in_(
            select(MetaTable.id).where(MetaTable.datasource_id == datasource_id)))
    metrics = (await session.execute(metrics_q)).scalars().all()
    dims = (await session.execute(dims_q)).scalars().all()

    # ---- 1. 重复/疑似重复指标（同名或别名交叉） ----
    #    seen 记「名字 → 第一个占用者」：跨表同名指标会自动带上表后缀，
    #    但用户维护的别名常常撞车，这里把后来者全部报出来提示合并。
    seen: dict[str, object] = {}
    for m in metrics:
        names = {m.name.lower()} | {a.lower() for a in (m.aliases or [])}
        for n in names:
            if n in seen and str(getattr(seen[n], "id", "")) != str(m.id):
                other = seen[n]
                issues.append({
                    "category": "duplicate_metric", "severity": "warn",
                    "entity_type": "metric", "entity_id": str(m.id), "entity_name": m.name,
                    "message": f"指标「{m.name}」与「{getattr(other, 'name', '?')}」名称/别名交叉，疑似口径重复，建议合并",
                })
            seen.setdefault(n, m)

    # ---- 2. 字段注释缺失（自动识别质量） ----
    col_q = select(MetaColumn).join(MetaTable, MetaColumn.table_id == MetaTable.id).where(
        MetaColumn.source == SourceKind.AUTO,
        MetaColumn.comment == "",
    )
    if datasource_id:
        col_q = col_q.where(MetaTable.datasource_id == datasource_id)
    for c in (await session.execute(col_q.limit(200))).scalars():
        if c.semantic_role == SemanticRole.UNKNOWN:
            issues.append({
                "category": "missing_comment", "severity": "warn",
                "entity_type": "column", "entity_id": str(c.id), "entity_name": c.column_name,
                "message": f"字段 {c.column_name} 无注释且语义未识别，将不出现在图谱中，建议补注释或人工分类",
            })

    # ---- 3. 指标缺少聚合规则 / 缺业务口径 ----
    for m in metrics:
        if not m.agg_function:
            issues.append({
                "category": "metric_agg_missing", "severity": "warn",
                "entity_type": "metric", "entity_id": str(m.id), "entity_name": m.name,
                "message": f"指标「{m.name}」缺少聚合规则（sum/count/avg），影响 SQL 生成准确性",
            })
        if m.source == SourceKind.AUTO and m.business_def.startswith("【自动生成】"):
            issues.append({
                "category": "metric_def_placeholder", "severity": "warn",
                "entity_type": "metric", "entity_id": str(m.id), "entity_name": m.name,
                "message": f"指标「{m.name}」业务口径为自动占位，建议人工补充口径定义",
            })

    # ---- 4. 非法维度组合预检测：显式 FORBIDDEN 边是否与 ALLOWED 冲突 ----
    forbidden = (await session.execute(
        select(KgEdge).where(KgEdge.edge_type == EdgeType.METRIC_DIM_FORBIDDEN)
    )).scalars().all()
    # ALLOWED 边一次性取成 (指标id, 维度id) 集合做内存比对，替代每条 FORBIDDEN 边一次冲突查询
    allowed_pairs = {(str(src), str(dst)) for src, dst in (await session.execute(
        select(KgEdge.src_id, KgEdge.dst_id).where(
            KgEdge.edge_type == EdgeType.METRIC_DIM_ALLOWED,
            KgEdge.src_type == EntityType.METRIC,
            KgEdge.dst_type == EntityType.DIMENSION))).all()}
    for e in forbidden:
        if (str(e.src_id), str(e.dst_id)) in allowed_pairs:
            issues.append({
                "category": "conflict_dim_rule", "severity": "error",
                "entity_type": "metric", "entity_id": str(e.src_id), "entity_name": "",
                "message": "同一指标对同一维度同时存在 ALLOWED 与 FORBIDDEN 约束，请修正规则",
            })

    # ---- 5. 非法维度组合预检测：白名单登记了、但物理上永远跑不通的组合 ----
    #    白名单是人工登记的，容易和实际表结构脱节。「指标挂在 ads 汇总表、
    #    白名单却写了地区维度」这种组合在问答时才会炸，这里提前在图谱质量页暴露出来。
    issues.extend(await _check_unreachable_combos(session, metrics))

    # ---- 6. 用户自定义规则（值域等）语法检查 ----
    rules = (await session.execute(select(KgValidationRule).where(KgValidationRule.enabled.is_(True)))).scalars().all()
    for r in rules:
        if r.rule_type == RuleType.METRIC_VALUE_RANGE:
            vr = r.rule_config.get("range")
            if not (isinstance(vr, list) and len(vr) == 2):
                issues.append({
                    "category": "invalid_rule_config", "severity": "error",
                    "entity_type": "rule", "entity_id": str(r.id), "entity_name": r.rule_type.value,
                    "message": "值域规则配置非法：rule_config.range 应为 [min, max]",
                })

    return issues


async def _check_unreachable_combos(session, metrics: list[KgMetric]) -> list[dict]:
    """白名单登记了、但指标所在表拿不到该维度列的组合。

    判据与问答链路一致（service._metric_serves_dims）：维度列在本表上，
    或在 TABLE_JOIN 一跳可达的维表上。这里只报错不改数据 —— 修法是
    「补表关系」或「删掉这条白名单边」，属于人工决策。
    """
    from app.kg import service as kg_service  # 延迟导入：service 不依赖 validator，此处仅为避免顶层耦合

    out: list[dict] = []
    # 调用级图谱缓存：白名单边 / TABLE_JOIN 邻接 / 维度表各查一次，
    # 替代每指标（甚至每指标×每维度）一轮全量查询
    cache = kg_service._GraphCache()
    by_id = await cache.dim_by_id(session)
    for m in metrics:
        allowed = await kg_service._allowed_dim_ids(session, m, cache)
        if not allowed:
            continue
        if await kg_service._metric_serves_dims(session, m, set(allowed), cache):
            continue
        # 定位到底哪些维度够不着，报错信息要能直接指到维度名
        unreachable = []
        for did in allowed:
            d = by_id.get(did)
            if d is None:
                continue
            if not await kg_service._metric_serves_dims(session, m, {did}, cache):
                unreachable.append(d.name)
        if not unreachable:
            continue
        out.append({
            "category": "unreachable_dim_combo", "severity": "error",
            "entity_type": "metric", "entity_id": str(m.id), "entity_name": m.name,
            "message": f"指标「{m.name}」的白名单包含 {'、'.join(unreachable)}，"
                       "但所在表既没有这些维度列、也无法通过表关联取得，该组合查不出结果；"
                       "请补充表关系或调整白名单",
        })
    return out


async def ensure_default_rules(session) -> None:
    """初始化默认全局规则（幂等）。"""
    exists = (await session.execute(
        select(KgValidationRule).where(KgValidationRule.target_type == "global").limit(1)
    )).scalar_one_or_none()
    if exists:
        return
    session.add(KgValidationRule(
        rule_type=RuleType.METRIC_AGG_REQUIRED, target_type="global",
        severity=RuleSeverity.WARN, message="所有指标必须配置聚合规则",
        source=SourceKind.MANUAL,
    ))
    session.add(KgValidationRule(
        rule_type=RuleType.METRIC_DEF_REQUIRED, target_type="global",
        severity=RuleSeverity.WARN, message="所有指标必须具备业务口径定义",
        source=SourceKind.MANUAL,
    ))
