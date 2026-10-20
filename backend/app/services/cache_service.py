"""查询缓存：高频查询毫秒级返回（PRD 4.1）。"""
from __future__ import annotations

import hashlib
from datetime import timedelta

from sqlalchemy import delete, select

from app.core.config import settings
from app.db.models import QueryCache
from app.db.models.base import utcnow


def cache_key(datasource_id: str, sql: str) -> str:
    raw = f"{datasource_id}|{sql.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]


async def get_cache(session, key: str) -> dict | None:
    row = (await session.execute(
        select(QueryCache).where(QueryCache.cache_key == key)
    )).scalar_one_or_none()
    if row is None:
        return None
    if row.expires_at and row.expires_at < utcnow():
        return None
    row.hit_count += 1
    return {"result": row.result, "chart_spec": row.chart_spec, "answer_text": row.answer_text}


async def set_cache(session, key: str, result: dict, chart_spec: dict | None,
                    answer_text: str = "") -> None:
    existing = (await session.execute(
        select(QueryCache).where(QueryCache.cache_key == key)
    )).scalar_one_or_none()
    expires = utcnow() + timedelta(seconds=settings.query_cache_ttl_sec)
    if existing is None:
        session.add(QueryCache(cache_key=key, result=result, chart_spec=chart_spec,
                               answer_text=answer_text, expires_at=expires))
    else:
        existing.result = result
        existing.chart_spec = chart_spec
        existing.answer_text = answer_text
        existing.expires_at = expires


async def purge_expired(session) -> int:
    res = await session.execute(
        delete(QueryCache).where(QueryCache.expires_at < utcnow()))
    return res.rowcount or 0
