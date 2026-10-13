"""审计日志（admin）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.db.models import AuditLog
from app.db.session import get_db

router = APIRouter(prefix="/audit-logs", tags=["audit"])


@router.get("")
async def list_audit_logs(action: str | None = None, username: str | None = None,
                          page: int = 1, page_size: int = 50,
                          db: AsyncSession = Depends(get_db), _=Depends(require_admin)):
    page, page_size = max(page, 1), min(max(page_size, 1), 200)

    q = select(AuditLog).order_by(AuditLog.created_at.desc())
    if action:
        q = q.where(AuditLog.action == action)
    if username:
        q = q.where(AuditLog.username == username)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    rows = (await db.execute(q.offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"total": total, "page": page, "rows": [
        {"id": str(r.id), "username": r.username, "action": r.action,
         "resource_type": r.resource_type, "resource_id": r.resource_id,
         "detail": r.detail, "ip": r.ip, "created_at": r.created_at.isoformat()} for r in rows]}
