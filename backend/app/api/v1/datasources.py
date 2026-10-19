"""数据源管理：CRUD / 连通性测试 / 扫描触发 / 元数据浏览。"""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import accessible_datasource_ids, get_current_user, require_admin
from app.connectors.registry import get_connector
from app.core.crypto import decrypt, encrypt
from app.core.errors import AppError, DatasourceError, NotFoundError, PermissionDenied, ValidationError
from app.db.models import (AuditLog, DataSource, DsStatus, DbType, MaskStrategy, MaskingRule,
                           MetaColumn, MetaTable, ScanJob, ScanJobStatus, ScanMode, SourceKind)
from app.db.models.base import utcnow
from app.db.session import get_db, get_session_factory
from app.metadata.scanner import scan_datasource
from app.schemas.entities import DatasourceIn, DatasourceOut, DatasourceUpdateIn

router = APIRouter(prefix="/datasources", tags=["datasources"])

VALID_TYPES = {t.value for t in DbType}

# 同一数据源的扫描互斥（进程内）。选择「冲突即拒」而非排队：扫描是分钟级重活，
# 排队语义对前端不友好；调用方收到 409 后可稍候重试。
# 注意：定时扫描（jobs.scheduler）也会落 RUNNING 状态的 ScanJob 行，下方触发前的
# RUNNING 检查同样能挡住与进行中定时扫描的冲突；定时任务自身由调度器
# max_instances=1 保证不并发。
_scan_locks: dict[str, asyncio.Lock] = {}
# 进行中的 RUNNING 任务超过该时长视为悬挂（进程崩溃遗留），不再阻塞新触发；
# 启动时由 bootstrap.reap_stale_scan_jobs 回收标记 FAILED
_SCAN_RUNNING_STALE_MINUTES = 30


async def _refresh_scan_jobs() -> None:
    """数据源增删改后同步定时扫描任务（调度器未启动时静默跳过）。"""
    try:
        from app.jobs.scheduler import refresh_scan_jobs
        await refresh_scan_jobs()
    except Exception:
        pass


def _validate_cron(expr: str | None) -> str | None:
    """校验 5 段 cron 表达式；空串归一为 None，非法表达式拒绝。"""
    expr = (expr or "").strip() or None
    if expr is None:
        return None
    from app.jobs.scheduler import _parse_cron
    if _parse_cron(expr) is None:
        raise ValidationError(f"scan_cron 非法（需为 5 段表达式：分 时 日 月 周）: {expr}")
    return expr


@router.get("", response_model=list[DatasourceOut])
async def list_datasources(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """登录用户可查：admin 看全部，其他角色只看被授权的数据源（普通用户建会话需要选数据源）。"""
    q = select(DataSource).order_by(DataSource.created_at)
    allowed = await accessible_datasource_ids(db, user)
    if allowed is not None:
        q = q.where(DataSource.id.in_(allowed))
    rows = (await db.execute(q)).scalars().all()
    return rows


@router.post("", response_model=DatasourceOut)
async def create_datasource(body: DatasourceIn, db: AsyncSession = Depends(get_db),
                            user=Depends(require_admin)):
    if body.db_type not in VALID_TYPES:
        raise ValidationError(f"不支持的类型；当前支持: {sorted(VALID_TYPES)}")
    exists = (await db.execute(select(DataSource).where(DataSource.name == body.name))).scalar_one_or_none()
    if exists:
        raise ValidationError("数据源名称已存在")
    ds = DataSource(
        name=body.name, db_type=DbType(body.db_type), host=body.host, port=body.port,
        database_name=body.database_name, username=body.username,
        extra_params=body.extra_params or {},
        table_whitelist=body.table_whitelist or [], table_blacklist=body.table_blacklist or [],
        scan_cron=_validate_cron(body.scan_cron),
        sched_write_enabled=body.sched_write_enabled,
        readonly_enforced=body.readonly_enforced)
    db.add(ds)
    await db.flush()
    ds.password_cipher = encrypt(body.password, aad=f"ds:{ds.id}") if body.password else ""
    db.add(AuditLog(user_id=user.id, username=user.username, action="datasource.create",
                    resource_type="datasource", resource_id=str(ds.id),
                    detail={"name": body.name, "db_type": body.db_type}))
    await db.commit()
    await db.refresh(ds)
    await _refresh_scan_jobs()
    return ds


@router.patch("/{ds_id}", response_model=DatasourceOut)
async def update_datasource(ds_id: str, body: DatasourceUpdateIn,
                            db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    ds = await db.get(DataSource, uuid.UUID(ds_id))
    if ds is None:
        raise NotFoundError("数据源不存在")
    changed = []
    for f in ("name", "host", "port", "database_name", "username", "scan_cron"):
        v = getattr(body, f)
        if v is not None:
            setattr(ds, f, _validate_cron(v) if f == "scan_cron" else v)
            changed.append(f)
    if body.extra_params is not None:
        ds.extra_params = body.extra_params
        changed.append("extra_params")
    if body.sched_write_enabled is not None:
        ds.sched_write_enabled = body.sched_write_enabled
        changed.append("sched_write_enabled")
    if body.readonly_enforced is not None:
        ds.readonly_enforced = body.readonly_enforced
        changed.append("readonly_enforced")
    if body.table_whitelist is not None:
        ds.table_whitelist = body.table_whitelist
    if body.table_blacklist is not None:
        ds.table_blacklist = body.table_blacklist
    if body.password:
        ds.password_cipher = encrypt(body.password, aad=f"ds:{ds.id}")
        changed.append("password")
    db.add(AuditLog(user_id=user.id, username=user.username, action="datasource.update",
                    resource_type="datasource", resource_id=str(ds.id), detail={"fields": changed}))
    await db.commit()
    await db.refresh(ds)
    if "scan_cron" in changed:
        await _refresh_scan_jobs()
    return ds


@router.delete("/{ds_id}")
async def delete_datasource(ds_id: str, db: AsyncSession = Depends(get_db),
                            user=Depends(require_admin)):
    ds = await db.get(DataSource, uuid.UUID(ds_id))
    if ds is None:
        raise NotFoundError("数据源不存在")
    if ds.is_builtin:
        raise PermissionDenied("内置演示数据源不可删除")
    db.add(AuditLog(user_id=user.id, username=user.username, action="datasource.delete",
                    resource_type="datasource", resource_id=str(ds.id), detail={"name": ds.name}))
    await db.delete(ds)
    await db.commit()
    from app.connectors.base import Connector
    Connector.drop_cached_engine(str(ds_id))   # 销毁连接池缓存，不留残留连接
    await _refresh_scan_jobs()
    return {"ok": True}


@router.post("/{ds_id}/test")
async def test_datasource(ds_id: str, db: AsyncSession = Depends(get_db),
                          user=Depends(require_admin)):
    ds = await db.get(DataSource, uuid.UUID(ds_id))
    if ds is None:
        raise NotFoundError("数据源不存在")
    try:
        connector = get_connector(ds)
        ok, msg = await connector.test()
    except DatasourceError as e:
        ok, msg = False, e.message
    ds.status = DsStatus.CONNECTED if ok else DsStatus.ERROR
    ds.status_message = msg
    ds.last_test_at = utcnow()
    await db.commit()
    return {"ok": ok, "message": msg}


@router.post("/{ds_id}/scan")
async def trigger_scan(ds_id: str, body: dict, background: BackgroundTasks,
                       db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    mode = ScanMode((body or {}).get("mode", "full"))
    ds = await db.get(DataSource, uuid.UUID(ds_id))
    if ds is None:
        raise NotFoundError("数据源不存在")

    # 互斥检查 1：进程内锁（手动触发之间；锁由后台扫描任务持有到扫描结束）
    lock = _scan_locks.setdefault(str(ds.id), asyncio.Lock())
    if lock.locked():
        raise AppError("该数据源正在扫描中，请稍候再试",
                       code="SCAN_IN_PROGRESS", http_status=409)
    # 互斥检查 2：进行中的 RUNNING 任务（覆盖定时扫描 / 其他触发途径）；
    # 超过悬挂阈值的 RUNNING 视为崩溃遗留，不阻塞新触发
    stale_before = utcnow() - timedelta(minutes=_SCAN_RUNNING_STALE_MINUTES)
    running = (await db.execute(select(ScanJob).where(
        ScanJob.datasource_id == ds.id, ScanJob.status == ScanJobStatus.RUNNING,
        ScanJob.started_at.isnot(None), ScanJob.started_at >= stale_before
    ))).scalars().first()
    if running is not None:
        raise AppError("该数据源正在扫描中，请稍候再试",
                       code="SCAN_IN_PROGRESS", http_status=409)

    await lock.acquire()
    try:
        job = ScanJob(datasource_id=ds.id, mode=mode, triggered_by=f"user:{user.id}",
                      status=ScanJobStatus.RUNNING, started_at=utcnow())
        db.add(job)
        await db.flush()
        job_id = str(job.id)
        await db.commit()
    except Exception:
        lock.release()
        raise

    async def _run():
        try:
            async with get_session_factory()() as session:
                ds2 = await session.get(DataSource, uuid.UUID(ds_id))
                job2 = await session.get(ScanJob, uuid.UUID(job_id))
                if ds2 is None or job2 is None:
                    return   # 并发删除：直接退出，不再触碰 None
                try:
                    await scan_datasource(session, ds2, mode, job2)
                    await session.commit()
                except Exception as e:
                    job2.status = ScanJobStatus.FAILED
                    job2.error_log = [str(e)[:500]]
                    job2.finished_at = utcnow()
                    await session.commit()
        finally:
            lock.release()

    background.add_task(_run)
    return {"scan_job_id": job_id}


# ---------- 元数据浏览 ----------

@router.get("/{ds_id}/tables")
async def list_tables(ds_id: str, db: AsyncSession = Depends(get_db),
                      user=Depends(require_admin)):
    rows = (await db.execute(
        select(MetaTable).where(MetaTable.datasource_id == uuid.UUID(ds_id))
        .order_by(MetaTable.table_name))).scalars().all()
    return [{"id": str(r.id), "table_name": r.table_name, "comment": r.comment,
             "layer": r.layer.value, "row_count": r.row_count,
             "partition_cols": r.partition_cols, "source": r.source.value,
             "last_scan_at": r.last_seen_at.isoformat() if r.last_seen_at else None}
            for r in rows]


@router.get("/tables/{table_id}/columns")
async def list_columns(table_id: str, db: AsyncSession = Depends(get_db),
                       user=Depends(require_admin)):
    rows = (await db.execute(
        select(MetaColumn).where(MetaColumn.table_id == uuid.UUID(table_id))
        .order_by(MetaColumn.column_name))).scalars().all()
    return [{"id": str(r.id), "column_name": r.column_name, "data_type": r.data_type,
             "comment": r.comment, "is_primary_key": r.is_primary_key,
             "semantic_role": r.semantic_role.value, "agg_hint": r.agg_hint,
             "value_examples": r.value_examples, "source": r.source.value} for r in rows]


@router.patch("/columns/{column_id}")
async def update_column(column_id: str, body: dict, db: AsyncSession = Depends(get_db),
                        user=Depends(require_admin)):
    """人工修正字段分类（semantic_role/agg_hint/comment）。"""
    col = await db.get(MetaColumn, uuid.UUID(column_id))
    if col is None:
        raise NotFoundError("字段不存在")
    editable = [f for f in ("semantic_role", "agg_hint", "comment") if f in (body or {})]
    from app.kg.merger import mark_edited
    if "semantic_role" in editable:
        from app.db.models.metadata import SemanticRole
        try:
            body["semantic_role"] = SemanticRole(body["semantic_role"])
        except ValueError:
            raise ValidationError(f"semantic_role 非法：{body['semantic_role']}")
    for f in editable:
        setattr(col, f, body[f])
    if editable:
        mark_edited(col, editable)
    await db.commit()
    return {"ok": True, "edited_fields": col.edited_fields}


@router.patch("/tables/{table_id}")
async def update_table_meta(table_id: str, body: dict, db: AsyncSession = Depends(get_db),
                            user=Depends(require_admin)):
    """人工修正表信息（layer/comment）。"""
    t = await db.get(MetaTable, uuid.UUID(table_id))
    if t is None:
        raise NotFoundError("表不存在")
    editable = [f for f in ("layer", "comment") if f in (body or {})]
    from app.kg.merger import mark_edited
    if "layer" in editable:
        from app.db.models.metadata import TableLayer
        try:
            body["layer"] = TableLayer(str(body["layer"]).lower())
        except ValueError:
            raise ValidationError(f"layer 非法：{body['layer']}")
    for f in editable:
        setattr(t, f, body[f])
    if editable:
        mark_edited(t, editable)
    await db.commit()
    return {"ok": True}


# ---------- 敏感数据脱敏规则 ----------

VALID_STRATEGIES = {s.value for s in MaskStrategy}


def _mask_rule_out(r: MaskingRule) -> dict:
    return {"id": str(r.id), "datasource_id": str(r.datasource_id),
            "column_pattern": r.column_pattern, "strategy": r.strategy.value,
            "enabled": r.enabled, "created_at": r.created_at.isoformat()}


@router.get("/{ds_id}/masking-rules")
async def list_masking_rules(ds_id: str, db: AsyncSession = Depends(get_db),
                             user=Depends(require_admin)):
    rows = (await db.execute(select(MaskingRule).where(
        MaskingRule.datasource_id == uuid.UUID(ds_id))
        .order_by(MaskingRule.created_at))).scalars().all()
    return [_mask_rule_out(r) for r in rows]


@router.post("/{ds_id}/masking-rules")
async def create_masking_rule(ds_id: str, body: dict, db: AsyncSession = Depends(get_db),
                              user=Depends(require_admin)):
    """body: {column_pattern, strategy, enabled}"""
    ds = await db.get(DataSource, uuid.UUID(ds_id))
    if ds is None:
        raise NotFoundError("数据源不存在")
    pattern = str((body or {}).get("column_pattern") or "").strip()
    strategy = str((body or {}).get("strategy") or "")
    if not pattern or len(pattern) > 128:
        raise ValidationError("column_pattern 不能为空且不超过128字符")
    if strategy not in VALID_STRATEGIES:
        raise ValidationError(f"strategy 非法，可选: {sorted(VALID_STRATEGIES)}")
    r = MaskingRule(datasource_id=ds.id, column_pattern=pattern,
                    strategy=MaskStrategy(strategy), enabled=bool(body.get("enabled", True)))
    db.add(r)
    await db.flush()
    db.add(AuditLog(user_id=user.id, username=user.username, action="masking.create",
                    resource_type="masking_rule", resource_id=str(r.id),
                    detail={"datasource_id": ds_id, "column_pattern": pattern, "strategy": strategy}))
    await db.commit()
    await db.refresh(r)
    return _mask_rule_out(r)


@router.patch("/masking-rules/{rule_id}")
async def update_masking_rule(rule_id: str, body: dict, db: AsyncSession = Depends(get_db),
                              user=Depends(require_admin)):
    r = await db.get(MaskingRule, uuid.UUID(rule_id))
    if r is None:
        raise NotFoundError("脱敏规则不存在")
    if "column_pattern" in (body or {}):
        pattern = str(body["column_pattern"] or "").strip()
        if not pattern or len(pattern) > 128:
            raise ValidationError("column_pattern 不能为空且不超过128字符")
        r.column_pattern = pattern
    if "strategy" in (body or {}):
        if body["strategy"] not in VALID_STRATEGIES:
            raise ValidationError(f"strategy 非法，可选: {sorted(VALID_STRATEGIES)}")
        r.strategy = MaskStrategy(body["strategy"])
    if "enabled" in (body or {}):
        r.enabled = bool(body["enabled"])
    db.add(AuditLog(user_id=user.id, username=user.username, action="masking.update",
                    resource_type="masking_rule", resource_id=str(r.id),
                    detail={"fields": sorted(body or {})}))
    await db.commit()
    return _mask_rule_out(r)


@router.delete("/masking-rules/{rule_id}")
async def delete_masking_rule(rule_id: str, db: AsyncSession = Depends(get_db),
                              user=Depends(require_admin)):
    r = await db.get(MaskingRule, uuid.UUID(rule_id))
    if r is None:
        raise NotFoundError("脱敏规则不存在")
    db.add(AuditLog(user_id=user.id, username=user.username, action="masking.delete",
                    resource_type="masking_rule", resource_id=str(r.id),
                    detail={"column_pattern": r.column_pattern}))
    await db.delete(r)
    await db.commit()
    return {"ok": True}
