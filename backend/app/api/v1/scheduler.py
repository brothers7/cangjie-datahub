"""内置调度平台 API（PRD 3.3G）：运行实例 / 手动触发 / 失败重跑 / 审核闸门 / DAG 总览。

安全：审核仅管理员；触发与重跑走与定时调度一致的双重闸门（审核 + 数据源写入开关）；
所有动作审计。DDL 永不自动执行。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.errors import NotFoundError, PermissionDenied, ValidationError
from app.db.models import (AuditLog, EtlTask, Role, SchedulerRun, SchedulerRunStatus)
from app.db.models.base import utcnow
from app.db.session import get_db
from app.services import scheduler_engine as engine

router = APIRouter(prefix="/scheduler", tags=["scheduler"])


def _run_row(r: SchedulerRun, task: EtlTask | None = None) -> dict:
    return {"id": str(r.id), "task_id": str(r.task_id),
            "task_name": (task.name or task.target_table) if task else "",
            "target_table": task.target_table if task else "",
            "ds": r.ds, "status": r.status.value, "trigger_type": r.trigger_type,
            "message": r.message, "duration_ms": r.duration_ms,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "created_at": r.created_at.isoformat()}


async def _get_task(task_id: str, db: AsyncSession, user) -> EtlTask:
    t = await db.get(EtlTask, uuid.UUID(task_id))
    if t is None:
        raise NotFoundError("任务不存在")
    if user.role != Role.ADMIN and str(t.user_id) != str(user.id):
        raise PermissionDenied("无权操作该任务")
    return t


@router.get("/runs")
async def list_runs(task_id: str = "", limit: int = 50,
                    db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """运行实例列表（可按任务过滤）。任务可见性同任务权限。"""
    q = select(SchedulerRun).order_by(SchedulerRun.created_at.desc()).limit(min(limit, 200))
    if task_id:
        t = await _get_task(task_id, db, user)
        q = q.where(SchedulerRun.task_id == t.id)
    runs = (await db.execute(q)).scalars().all()
    task_ids = {r.task_id for r in runs}
    tasks = {str(t.id): t for t in (await db.execute(
        select(EtlTask).where(EtlTask.id.in_(task_ids)))).scalars()} if task_ids else {}
    out = []
    for r in runs:
        t = tasks.get(str(r.task_id))
        if t is None:
            continue
        if user.role != Role.ADMIN and str(t.user_id) != str(user.id):
            continue
        out.append(_run_row(r, t))
    return {"runs": out}


class TriggerIn(BaseModel):
    ds: str = ""
    cascade: bool = False  # 级联触发全部下游（拓扑序，仍受依赖与双重闸门约束）


@router.post("/tasks/{task_id}/trigger")
async def trigger_task(task_id: str, body: TriggerIn, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)):
    """手动触发（立即运行）。受双重闸门约束；人工确认在前端完成。"""
    t = await _get_task(task_id, db, user)
    if body.ds:
        import re
        from datetime import datetime as _dt
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", body.ds):
            raise ValidationError("业务日期格式应为 yyyy-mm-dd")
        try:
            _dt.strptime(body.ds, "%Y-%m-%d")
        except ValueError:
            raise ValidationError("业务日期不是有效日期")
    result = await engine.trigger(db, t, user, body.ds, cascade=body.cascade)
    await db.commit()
    if body.cascade:
        # 级联返回逐任务结果清单（首元素为本任务）
        return {"runs": result}
    return {"run": _run_row(result, t)}


@router.post("/runs/{run_id}/retry")
async def retry_run(run_id: str, db: AsyncSession = Depends(get_db),
                    user=Depends(get_current_user)):
    r = await db.get(SchedulerRun, uuid.UUID(run_id))
    if r is None:
        raise NotFoundError("运行实例不存在")
    t = await _get_task(str(r.task_id), db, user)
    if r.status not in (SchedulerRunStatus.FAILED, SchedulerRunStatus.BLOCKED,
                        SchedulerRunStatus.DEP_TIMEOUT):
        raise ValidationError("仅失败/阻塞/依赖超时的实例可重跑")
    new = await engine.retry(db, r, user)
    await db.commit()
    return {"run": _run_row(new, t)}


@router.post("/tasks/{task_id}/approve")
async def approve_task(task_id: str, db: AsyncSession = Depends(get_db),
                       user=Depends(require_admin)):
    """管理员审核通过：内置调度执行加工 SQL 的第一道闸门（第二道为数据源写入开关）。"""
    t = await db.get(EtlTask, uuid.UUID(task_id))
    if t is None:
        raise NotFoundError("任务不存在")
    t.approved_by = user.username
    t.approved_at = utcnow()
    db.add(AuditLog(user_id=user.id, username=user.username, action="scheduler.approve",
                    resource_type="etl_task", resource_id=task_id,
                    detail={"target_table": t.target_table}))
    await db.commit()
    return {"ok": True, "approved_by": t.approved_by,
            "approved_at": t.approved_at.isoformat()}


@router.get("/dag")
async def dag(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """调度 DAG 总览：节点=启用调度的任务，边=任务级依赖，附最近实例状态。"""
    q = select(EtlTask).where(EtlTask.sched_enabled.is_(True))
    if user.role != Role.ADMIN:
        q = q.where(EtlTask.user_id == user.id)
    tasks = (await db.execute(q)).scalars().all()
    ids = {str(t.id) for t in tasks}

    latest: dict[str, SchedulerRun] = {}
    if ids:
        runs = (await db.execute(select(SchedulerRun).where(
            SchedulerRun.task_id.in_(ids))
            .order_by(SchedulerRun.created_at.desc()))).scalars().all()
        for r in runs:
            latest.setdefault(str(r.task_id), r)

    nodes = [{"id": str(t.id), "name": t.name or t.target_table,
              "target_table": t.target_table, "layer": t.layer, "cron": t.cron,
              "approved": bool(t.approved_by),
              "last_status": latest[str(t.id)].status.value if str(t.id) in latest else None,
              "last_ds": latest[str(t.id)].ds if str(t.id) in latest else None}
             for t in tasks]
    edges = [{"from": d, "to": str(t.id)}
             for t in tasks for d in (t.depends_on or []) if str(d) in ids]
    return {"nodes": nodes, "edges": edges}
