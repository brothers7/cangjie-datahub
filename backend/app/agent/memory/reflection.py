"""反思记忆：夜间任务总结用户分析习惯、沉淀报表模板（PRD 2.1）。"""
from __future__ import annotations

import logging
from collections import Counter

from sqlalchemy import select

from app.db.models import QueryRecord, ReportTemplate, UserMemoryProfile
from app.db.models.llm import LlmScene
from app.llm.gateway import gateway
from app.llm.types import ChatMessage

logger = logging.getLogger(__name__)

REFLECT_PROMPT = """你是用户行为分析师。根据以下用户近 7 天的查数记录，总结该用户的数据分析习惯。
输出不超过 5 条要点，每条一行，聚焦：关注的业务域、常用指标、偏好维度、分析深度。
禁止输出记录中没有的推测之外的敏感信息。"""


async def reflect_user(session, user_id, display_name: str = "") -> str:
    """总结单个用户近 7 天查询习惯，更新 habit_summary。返回总结文本。"""
    from datetime import timedelta
    from app.db.models.base import utcnow

    since = utcnow() - timedelta(days=7)
    rows = (await session.execute(
        select(QueryRecord)
        .where(QueryRecord.user_id == user_id, QueryRecord.created_at >= since,
               QueryRecord.status.in_(["success", "empty"]))
        .order_by(QueryRecord.created_at.desc()).limit(60)
    )).scalars().all()
    if not rows:
        return ""
    lines = [f"- {r.question}（{r.chart_type or '无图'}，{r.duration_ms}ms）" for r in rows[:40]]
    try:
        resp = await gateway.chat(LlmScene.REFLECTION,
                                  [ChatMessage("system", REFLECT_PROMPT),
                                   ChatMessage("user", "\n".join(lines))],
                                  temperature=0.2)
        summary = resp.text.strip()[:2000]
    except Exception as e:
        logger.info("reflection LLM failed: %s", e)
        summary = ""

    profile = (await session.execute(
        select(UserMemoryProfile).where(UserMemoryProfile.user_id == user_id)
    )).scalar_one_or_none()
    if profile is None:
        profile = UserMemoryProfile(user_id=user_id)
        session.add(profile)
    if summary:
        profile.habit_summary = summary
    return summary


async def sediment_report_templates(session) -> int:
    """同类查询（同 sql_hash）≥3 次 → 沉淀为报表模板。"""
    from sqlalchemy import func

    rows = (await session.execute(
        select(QueryRecord.sql_hash, func.count(QueryRecord.id).label("c"))
        .where(QueryRecord.status == "success", QueryRecord.sql_hash != "")
        .group_by(QueryRecord.sql_hash).having(func.count(QueryRecord.id) >= 3)
    )).all()
    created = 0
    for sql_hash, cnt in rows:
        exists = (await session.execute(
            select(ReportTemplate).where(ReportTemplate.structure.contains({"sql_hash": sql_hash}))
        )).scalar_one_or_none()
        if exists:
            continue
        sample = (await session.execute(
            select(QueryRecord).where(QueryRecord.sql_hash == sql_hash)
            .order_by(QueryRecord.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        if sample is None:
            continue
        session.add(ReportTemplate(
            name=f"常用查询模板: {sample.question[:40]}",
            structure={"sql_hash": sql_hash, "question": sample.question,
                       "matched_metric_ids": sample.matched_metric_ids,
                       "used_dimensions": sample.used_dimensions,
                       "chart_type": sample.chart_type},
            source="reflection", usage_count=cnt,
            created_from_user_id=sample.user_id,
        ))
        created += 1
    return created
