"""知识图谱：可视化数据 / 指标与维度 CRUD / 合并 / 约束规则 / 校验报告。"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import accessible_datasource_ids, get_current_user, require_admin
from app.core.errors import NotFoundError, PermissionDenied, ValidationError
from app.db.models import (EdgeType, EntityType, KgDimension, KgEdge, KgMetric, KgValidationRule,
                           DimensionType, MetaTable, MetricStatus, RuleSeverity, RuleType, SourceKind)
from app.db.session import get_db
from app.kg.merger import mark_edited
from app.kg.service import metric_context
from app.kg.validator import run_validation
from app.schemas.entities import (DimensionUpdateIn, MetricCreateIn, MetricMergeIn,
                                  MetricUpdateIn, RuleIn)

router = APIRouter(prefix="/kg", tags=["kg"])

# analyst 也可查看图谱（在路由函数内放开）；写操作走 require_admin


@router.get("/graph")
async def get_graph(datasource_id: str | None = None, q: str | None = None,
                    types: str = "metric,dimension,table", limit: int = 400,
                    db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    import uuid as _u

    want = set((types or "").split(","))
    nodes, edges_out = [], []

    tables_q = select(MetaTable)
    if datasource_id:
        tables_q = tables_q.where(MetaTable.datasource_id == _u.UUID(datasource_id))
    allowed = await accessible_datasource_ids(db, user)   # 非 admin 只看有权数据源
    if allowed is not None:
        if datasource_id:
            ds_filter = ([_u.UUID(datasource_id)]
                         if _u.UUID(datasource_id) in allowed else [])
        else:
            ds_filter = allowed
        tables_q = tables_q.where(MetaTable.datasource_id.in_(ds_filter))
    tables = {str(t.id): t for t in (await db.execute(tables_q)).scalars()}

    if "metric" in want:
        mq = select(KgMetric).where(KgMetric.status == MetricStatus.ACTIVE)
        if datasource_id or allowed is not None:
            mq = mq.where(KgMetric.table_id.in_([_u.UUID(x) for x in tables]))
        for m in (await db.execute(mq.limit(limit))).scalars():
            nodes.append({"id": str(m.id), "name": m.name, "type": "metric",
                          "business_domain": m.business_domain, "agg": m.agg_function,
                          "unit": m.unit, "source": m.source.value,
                          "table_id": str(m.table_id) if m.table_id else None})
    if "dimension" in want:
        for d in (await db.execute(select(KgDimension).where(
                KgDimension.status == MetricStatus.ACTIVE).limit(limit))).scalars():
            nodes.append({"id": str(d.id), "name": d.name, "type": "dimension",
                          "dimension_type": d.dimension_type.value, "source": d.source.value})
    if "table" in want:
        for tid, t in tables.items():
            nodes.append({"id": tid, "name": t.table_name, "type": "table",
                          "layer": t.layer.value, "comment": t.comment[:60]})

    if q:  # 名称过滤（高亮用）
        ql = q.lower()
        nodes = [n for n in nodes if ql in n["name"].lower()]

    node_ids = {n["id"] for n in nodes}
    type_by_id = {n["id"]: n["type"] for n in nodes}
    for e in (await db.execute(select(KgEdge).limit(2000))).scalars():
        sid, did = str(e.src_id), str(e.dst_id)
        if sid in node_ids and did in node_ids:
            edges_out.append({"src": sid, "src_type": type_by_id[sid],
                              "dst": did, "dst_type": type_by_id[did],
                              "edge_type": e.edge_type.value,
                              "properties": e.properties})
    return {"nodes": nodes, "edges": edges_out}


# ---------- 指标 ----------

@router.get("/metrics")
async def list_metrics(datasource_id: str | None = None, status: str | None = None,
                       domain: str | None = None, q: str | None = None,
                       db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    import uuid as _u
    query = select(KgMetric).order_by(KgMetric.name)
    tables = None
    allowed = await accessible_datasource_ids(db, user)
    if datasource_id:
        # 显式指定数据源也要校验权限（非 admin 越权指定时视为无权限 → 空集）
        if allowed is not None and _u.UUID(datasource_id) not in allowed:
            raise PermissionDenied("没有该数据源的访问权限")
        tables = select(MetaTable.id).where(MetaTable.datasource_id == _u.UUID(datasource_id))
    elif allowed is not None:   # 非 admin 只看有权数据源的指标
        tables = select(MetaTable.id).where(MetaTable.datasource_id.in_(allowed))
    if tables is not None:
        query = query.where(KgMetric.table_id.in_(tables))
    if status:
        query = query.where(KgMetric.status == MetricStatus(status))
    if domain:
        query = query.where(KgMetric.business_domain == domain)
    rows = (await db.execute(query)).scalars().all()
    out = [_metric_out(m) for m in rows]
    if q:
        ql = q.lower()
        out = [m for m in out if ql in m["name"].lower()
               or any(ql in a.lower() for a in m["aliases"]) or ql in (m["business_def"] or "").lower()]
    return out


def _metric_out(m: KgMetric) -> dict:
    return {"id": str(m.id), "name": m.name, "aliases": m.aliases or [],
            "business_def": m.business_def, "formula": m.formula,
            "agg_function": m.agg_function, "unit": m.unit, "value_range": m.value_range,
            "period_grain": m.period_grain, "business_domain": m.business_domain,
            "table_id": str(m.table_id) if m.table_id else None,
            "status": m.status.value, "confidence": m.confidence,
            "source": m.source.value, "edited_fields": m.edited_fields or []}


@router.post("/metrics")
async def create_metric(body: MetricCreateIn, db: AsyncSession = Depends(get_db),
                        user=Depends(require_admin)):
    exists = (await db.execute(select(KgMetric).where(KgMetric.name == body.name))).scalar_one_or_none()
    if exists:
        raise ValidationError("指标名已存在")
    m = KgMetric(name=body.name, aliases=body.aliases, business_def=body.business_def,
                 formula=body.formula, agg_function=body.agg_function, unit=body.unit,
                 value_range=body.value_range, business_domain=body.business_domain,
                 table_id=uuid.UUID(body.table_id) if body.table_id else None,
                 column_id=uuid.UUID(body.column_id) if body.column_id else None,
                 source=SourceKind.MANUAL, confidence=1.0)
    db.add(m)
    await db.commit()
    return _metric_out(m)


@router.patch("/metrics/{metric_id}")
async def update_metric(metric_id: str, body: MetricUpdateIn,
                        db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    m = await db.get(KgMetric, uuid.UUID(metric_id))
    if m is None:
        raise NotFoundError("指标不存在")
    editable = ["name", "aliases", "business_def", "formula", "agg_function", "unit",
                "value_range", "period_grain", "business_domain", "status"]
    changed = [f for f in editable if getattr(body, f) is not None]
    if "status" in changed:
        try:
            body.status = MetricStatus(body.status).value
        except ValueError:
            raise ValidationError(f"status 非法：{body.status}")
    if "name" in changed:
        dup = (await db.execute(select(KgMetric).where(
            KgMetric.name == body.name, KgMetric.id != m.id))).scalar_one_or_none()
        if dup:
            raise ValidationError("指标名已存在")
    for f in changed:
        setattr(m, f, getattr(body, f))
    if changed:
        mark_edited(m, changed)   # 人工口径保护
        m.lock_version += 1
    await db.commit()
    return _metric_out(m)


@router.delete("/metrics/{metric_id}")
async def deprecate_metric(metric_id: str, db: AsyncSession = Depends(get_db),
                           user=Depends(require_admin)):
    """软删除（废弃）：tombstone，重扫不复活。"""
    m = await db.get(KgMetric, uuid.UUID(metric_id))
    if m is None:
        raise NotFoundError("指标不存在")
    m.status = MetricStatus.DEPRECATED
    mark_edited(m, ["status"])
    await db.commit()
    return {"ok": True}


@router.post("/metrics/{metric_id}/merge")
async def merge_metric(metric_id: str, body: MetricMergeIn,
                       db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    src = await db.get(KgMetric, uuid.UUID(metric_id))
    dst = await db.get(KgMetric, uuid.UUID(body.target_metric_id))
    if src is None or dst is None:
        raise NotFoundError("指标不存在")
    if src.id == dst.id:
        raise ValidationError("不能合并到自身")
    # 别名并入目标，来源置 MERGED
    dst.aliases = list(dict.fromkeys((dst.aliases or []) + [src.name] + (src.aliases or [])))
    dst.aliases = [a for a in dst.aliases if a != dst.name][:20]
    src.status = MetricStatus.MERGED
    src.merged_into_id = dst.id
    mark_edited(dst, ["aliases"])
    mark_edited(src, ["status", "merged_into_id"])
    await db.commit()
    return {"ok": True}


@router.get("/metrics/{metric_id}/context")
async def metric_ctx(metric_id: str, db: AsyncSession = Depends(get_db), _=Depends(require_admin)):
    """调试：查看注入 LLM 的口径上下文。"""
    m = await db.get(KgMetric, uuid.UUID(metric_id))
    if m is None:
        raise NotFoundError("指标不存在")
    table = await db.get(MetaTable, m.table_id) if m.table_id else None
    text = await metric_context(db, [m], table, [])
    return {"context": text}


# ---------- 维度 ----------

@router.get("/dimensions")
async def list_dimensions(q: str | None = None, db: AsyncSession = Depends(get_db),
                          user=Depends(get_current_user)):
    rows = (await db.execute(select(KgDimension).order_by(KgDimension.name))).scalars().all()
    out = [_dim_out(d) for d in rows]
    if q:
        ql = q.lower()
        out = [d for d in out if ql in d["name"].lower()]
    return out


def _dim_out(d: KgDimension) -> dict:
    return {"id": str(d.id), "name": d.name, "aliases": d.aliases or [],
            "description": d.description, "dimension_type": d.dimension_type.value,
            "value_examples": d.value_examples or [], "related_columns": d.related_columns or [],
            "status": d.status.value, "source": d.source.value}


@router.patch("/dimensions/{dim_id}")
async def update_dimension(dim_id: str, body: DimensionUpdateIn,
                           db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    d = await db.get(KgDimension, uuid.UUID(dim_id))
    if d is None:
        raise NotFoundError("维度不存在")
    editable = ["name", "aliases", "description", "dimension_type", "value_examples", "status"]
    changed = [f for f in editable if getattr(body, f) is not None]
    if "status" in changed:
        try:
            body.status = MetricStatus(body.status).value
        except ValueError:
            raise ValidationError(f"status 非法：{body.status}")
    if "dimension_type" in changed:
        try:
            body.dimension_type = DimensionType(body.dimension_type).value
        except ValueError:
            raise ValidationError(f"dimension_type 非法：{body.dimension_type}")
    for f in changed:
        setattr(d, f, getattr(body, f))
    if changed:
        mark_edited(d, changed)
    await db.commit()
    return _dim_out(d)


@router.delete("/dimensions/{dim_id}")
async def deprecate_dimension(dim_id: str, db: AsyncSession = Depends(get_db),
                              user=Depends(require_admin)):
    d = await db.get(KgDimension, uuid.UUID(dim_id))
    if d is None:
        raise NotFoundError("维度不存在")
    d.status = MetricStatus.DEPRECATED
    mark_edited(d, ["status"])
    await db.commit()
    return {"ok": True}


# ---------- 约束规则 ----------

@router.get("/rules")
async def list_rules(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    rows = (await db.execute(select(KgValidationRule).order_by(KgValidationRule.rule_type))).scalars().all()
    return [{"id": str(r.id), "rule_type": r.rule_type.value, "target_type": r.target_type,
             "target_id": str(r.target_id) if r.target_id else None,
             "rule_config": r.rule_config, "severity": r.severity.value,
             "message": r.message, "enabled": r.enabled, "source": r.source.value} for r in rows]


@router.post("/rules")
async def create_rule(body: RuleIn, db: AsyncSession = Depends(get_db),
                      user=Depends(require_admin)):
    if body.rule_type not in {t.value for t in RuleType}:
        raise ValidationError("规则类型非法")
    r = KgValidationRule(
        rule_type=RuleType(body.rule_type), target_type=body.target_type,
        target_id=uuid.UUID(body.target_id) if body.target_id else None,
        rule_config=body.rule_config, severity=RuleSeverity(body.severity),
        message=body.message, enabled=body.enabled, source=SourceKind.MANUAL)
    db.add(r)
    await db.commit()
    return {"id": str(r.id)}


@router.patch("/rules/{rule_id}")
async def update_rule(rule_id: str, body: dict, db: AsyncSession = Depends(get_db),
                      user=Depends(require_admin)):
    r = await db.get(KgValidationRule, uuid.UUID(rule_id))
    if r is None:
        raise NotFoundError("规则不存在")
    for f in ("rule_config", "severity", "message", "enabled", "target_type"):
        if f in (body or {}):
            setattr(r, f, RuleSeverity(body[f]) if f == "severity" else body[f])
    await db.commit()
    return {"ok": True}


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str, db: AsyncSession = Depends(get_db),
                      user=Depends(require_admin)):
    r = await db.get(KgValidationRule, uuid.UUID(rule_id))
    if r is None:
        raise NotFoundError("规则不存在")
    await db.delete(r)
    await db.commit()
    return {"ok": True}


# ---------- 指标×维度 ALLOWED/FORBIDDEN 边（矩阵编辑） ----------

@router.post("/metric-dim-rule")
async def set_metric_dim_rule(body: dict, db: AsyncSession = Depends(get_db),
                              user=Depends(require_admin)):
    """body: {metric_id, dim_id, allowed: bool|null}；null=删除显式规则。"""
    mid, did = uuid.UUID(body["metric_id"]), uuid.UUID(body["dim_id"])
    for et in (EdgeType.METRIC_DIM_ALLOWED, EdgeType.METRIC_DIM_FORBIDDEN):
        hit = (await db.execute(select(KgEdge).where(
            KgEdge.edge_type == et, KgEdge.src_type == EntityType.METRIC, KgEdge.src_id == mid,
            KgEdge.dst_type == EntityType.DIMENSION, KgEdge.dst_id == did))).scalar_one_or_none()
        if hit is not None:
            await db.delete(hit)
    if body.get("allowed") is True:
        db.add(KgEdge(src_type=EntityType.METRIC, src_id=mid, dst_type=EntityType.DIMENSION,
                      dst_id=did, edge_type=EdgeType.METRIC_DIM_ALLOWED,
                      source=SourceKind.MANUAL))
    elif body.get("allowed") is False:
        db.add(KgEdge(src_type=EntityType.METRIC, src_id=mid, dst_type=EntityType.DIMENSION,
                      dst_id=did, edge_type=EdgeType.METRIC_DIM_FORBIDDEN,
                      source=SourceKind.MANUAL))
    await db.commit()
    return {"ok": True}


@router.get("/validation/report")
async def validation_report(datasource_id: str | None = None,
                            db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    import uuid as _u
    if datasource_id:
        allowed = await accessible_datasource_ids(db, user)
        if allowed is not None and _u.UUID(datasource_id) not in allowed:
            raise PermissionDenied("没有该数据源的访问权限")
    return await run_validation(db, _u.UUID(datasource_id) if datasource_id else None)


@router.post("/validation/run")
async def run_validation_now(datasource_id: str | None = None,
                             db: AsyncSession = Depends(get_db), _=Depends(require_admin)):
    import uuid as _u
    return await run_validation(db, _u.UUID(datasource_id) if datasource_id else None)
