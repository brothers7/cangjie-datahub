"""报表中心 / 查询记录。"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.errors import NotFoundError
from app.core.utils import like_contains
from app.db.models import QueryRecord, Report, ReportStatus, ReportTemplate, User
from app.db.session import get_db

router = APIRouter(tags=["reports"])


@router.get("/reports")
async def list_reports(q: str | None = None, page: int = 1, page_size: int = 20,
                       user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    page, page_size = max(page, 1), min(max(page_size, 1), 200)
    query = select(Report).where(Report.status == ReportStatus.ACTIVE)
    if user.role.value != "admin":
        query = query.where(Report.user_id == user.id)
    if q:
        like = like_contains(q)
        query = query.where(or_(Report.title.ilike(like, escape="\\"),
                                Report.content_md.ilike(like, escape="\\")))
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    rows = (await db.execute(
        query.order_by(Report.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"total": total, "page": page, "rows": [
        {"id": str(r.id), "title": r.title, "view_count": r.view_count,
         "created_at": r.created_at.isoformat(),
         "sections": [s.get("title") for s in (r.sections or [])]} for r in rows]}


@router.get("/reports/{report_id}")
async def get_report(report_id: str, user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)):
    r = await db.get(Report, uuid.UUID(report_id))
    if r is None or r.status != ReportStatus.ACTIVE:
        raise NotFoundError("报表不存在")
    if user.role.value != "admin" and r.user_id != user.id:
        raise NotFoundError("报表不存在")
    r.view_count += 1
    await db.commit()
    return {"id": str(r.id), "title": r.title, "content_md": r.content_md,
            "sections": r.sections, "view_count": r.view_count,
            "created_at": r.created_at.isoformat()}


@router.delete("/reports/{report_id}")
async def delete_report(report_id: str, user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    r = await db.get(Report, uuid.UUID(report_id))
    if r is None or (user.role.value != "admin" and r.user_id != user.id):
        raise NotFoundError("报表不存在")
    r.status = ReportStatus.DELETED
    await db.commit()
    return {"ok": True}


@router.get("/report-templates")
async def list_templates(user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ReportTemplate).order_by(
        ReportTemplate.usage_count.desc()))).scalars().all()
    return [{"id": str(r.id), "name": r.name, "source": r.source,
             "usage_count": r.usage_count, "structure": r.structure,
             "created_at": r.created_at.isoformat()} for r in rows]


# ---------- 查询记录 ----------

@router.get("/query-records")
async def list_query_records(mine: bool = True, status: str | None = None,
                             datasource_id: str | None = None, q: str | None = None,
                             page: int = 1, page_size: int = 20,
                             user: User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)):
    page, page_size = max(page, 1), min(max(page_size, 1), 200)

    query = select(QueryRecord)
    if mine or user.role.value != "admin":
        query = query.where(QueryRecord.user_id == user.id)
    if status:
        query = query.where(QueryRecord.status == status)
    if datasource_id:
        query = query.where(QueryRecord.datasource_id == uuid.UUID(datasource_id))
    if q:
        query = query.where(QueryRecord.question.ilike(like_contains(q), escape="\\"))
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    rows = (await db.execute(
        query.order_by(QueryRecord.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"total": total, "page": page, "rows": [
        {"id": str(r.id), "question": r.question, "rewritten_question": r.rewritten_question,
         "datasource_id": str(r.datasource_id) if r.datasource_id else None,
         "status": r.status.value, "row_count": r.row_count, "duration_ms": r.duration_ms,
         "cache_hit": r.cache_hit, "chart_type": r.chart_type,
         "sql_text": r.sql_text, "error_brief": r.error_brief,
         "created_at": r.created_at.isoformat()} for r in rows]}
