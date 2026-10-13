"""扫描任务查询。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.db.models import ScanJob
from app.db.session import get_db

router = APIRouter(prefix="/scan-jobs", tags=["scans"])


@router.get("")
async def list_scan_jobs(datasource_id: str | None = None, limit: int = 20,
                         db: AsyncSession = Depends(get_db), _=Depends(require_admin)):
    q = select(ScanJob).order_by(ScanJob.created_at.desc()).limit(min(limit, 100))
    if datasource_id:
        import uuid
        q = q.where(ScanJob.datasource_id == uuid.UUID(datasource_id))
    rows = (await db.execute(q)).scalars().all()
    return [_job_out(r) for r in rows]


@router.get("/{job_id}")
async def get_scan_job(job_id: str, db: AsyncSession = Depends(get_db), _=Depends(require_admin)):
    import uuid
    row = await db.get(ScanJob, uuid.UUID(job_id))
    if row is None:
        from app.core.errors import NotFoundError
        raise NotFoundError("任务不存在")
    return _job_out(row)


def _job_out(r: ScanJob) -> dict:
    return {
        "id": str(r.id), "datasource_id": str(r.datasource_id), "mode": r.mode.value,
        "triggered_by": r.triggered_by, "status": r.status.value, "progress": r.progress,
        "tables_total": r.tables_total, "tables_changed": r.tables_changed,
        "entities_created": r.entities_created, "entities_updated": r.entities_updated,
        "entities_skipped_manual": r.entities_skipped_manual,
        "error_log": r.error_log,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }
