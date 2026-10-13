"""实体链接：知识图谱指标/维度匹配（精确 → 别名 → 向量 → LLM 辅助兜底）。"""
from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.memory import long_term
from app.agent.nlu.time_parser import parse_time_range
from app.db.models import KgMetric, MetaTable, MetricStatus
from app.db.models.llm import LlmScene
from app.kg.service import Candidate, LinkedEntities, get_metric_details, link_entities
from app.llm.gateway import gateway
from app.llm.types import ChatMessage

logger = logging.getLogger(__name__)

LINK_PROMPT = """从用户问题中识别提到的数据指标。
指标候选（id|名称）：{metrics}
用户问题：{question}
只输出 JSON：{{"metric_ids": []}}，未提及输出空数组。不要解释。"""


async def link(session: AsyncSession, question: str, datasource_id,
               *, confirmed_metric_ids: list[str] | None = None,
               override_config_id: str | None = None) -> tuple[LinkedEntities, object | None]:
    """返回 (链接结果, 时间范围)。置信度不足时由管线发起 confirm_required。"""
    # 向量召回（embedding 未配置时返回空，自动降级）
    vector_hits: dict[str, list[float]] = {}
    try:
        for mid, score in (await long_term.search_metric_texts(question, top=5)).items():
            vector_hits.setdefault(mid, []).append(score)
    except Exception as e:
        logger.debug("vector link skipped: %s", e)

    linked = await link_entities(session, question, datasource_id,
                                 extra_metric_texts=vector_hits)

    # 已确认指标（多轮 confirm 续跑）直接置高置信
    if confirmed_metric_ids:
        confirm_set = set(confirmed_metric_ids)
        for c in linked.metrics:
            if c.id in confirm_set:
                c.confidence = 1.0
        if not any(c.id in confirm_set for c in linked.metrics):
            for m in await get_metric_details(session, confirmed_metric_ids):
                linked.metrics.append(Candidate("metric", str(m.id), m.name, 1.0,
                                                {"why": "用户已确认"}))

    # LLM 兜底：规则+向量都没匹配到指标时
    if not linked.metrics:
        linked = await _llm_assist(session, question, datasource_id, linked,
                                   override_config_id=override_config_id)

    time_range = parse_time_range(question)
    return linked, time_range


async def _llm_assist(session: AsyncSession, question: str, datasource_id,
                      linked: LinkedEntities, override_config_id: str | None = None) -> LinkedEntities:
    try:
        metric_ids = await _active_metric_ids(session, datasource_id)
        if not metric_ids:
            return linked
        metrics = await get_metric_details(session, metric_ids[:60])
        lines = [f"{m.id}|{m.name}（别名:{','.join((m.aliases or [])[:4])}）" for m in metrics]
        resp = await gateway.chat(LlmScene.ENTITY_LINK_ASSIST,
                                  [ChatMessage("user", LINK_PROMPT.format(
                                      metrics="\n".join(lines), question=question))],
                                  temperature=0.0, override_config_id=override_config_id)
        start, end = resp.text.find("{"), resp.text.rfind("}")
        data = json.loads(resp.text[start:end + 1])
        ids = [str(x) for x in data.get("metric_ids", [])][:5]
        for m in await get_metric_details(session, ids):
            linked.metrics.append(Candidate("metric", str(m.id), m.name, 0.75,
                                            {"why": "LLM辅助识别"}))
    except Exception as e:
        logger.debug("llm link assist failed: %s", e)
    return linked


async def _active_metric_ids(session, datasource_id) -> list[str]:
    q = select(KgMetric.id).where(KgMetric.status == MetricStatus.ACTIVE)
    if datasource_id:
        q = q.where(KgMetric.table_id.in_(
            select(MetaTable.id).where(MetaTable.datasource_id == datasource_id)))
    return [str(x) for x in (await session.execute(q)).scalars()]
