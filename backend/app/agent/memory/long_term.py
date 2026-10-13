"""长时记忆：Qdrant 语义检索（历史问题/指标文本）+ PG 用户画像。

embedding 未配置时全部优雅降级，不阻塞主链路。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select

from app.db.models import QueryRecord, UserMemoryProfile
from app.llm.gateway import gateway

logger = logging.getLogger(__name__)

COLL_QUERIES = "mem_queries"        # 历史问题 → 命中指标/SQL 模板
COLL_METRIC_TEXTS = "mem_metric_texts"  # 指标名+口径文本（实体链接辅助）

_client = None
_ready: dict[str, bool] = {}
_DIM_CACHE: int | None = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    try:
        from qdrant_client import AsyncQdrantClient
        from app.core.config import settings
        kwargs = {"url": settings.qdrant_url}
        if settings.qdrant_api_key:
            kwargs["api_key"] = settings.qdrant_api_key
        _client = AsyncQdrantClient(**kwargs, timeout=5)
        return _client
    except Exception as e:
        logger.info("Qdrant 不可用，长时记忆降级: %s", e)
        _client = False
        return None


async def _ensure_collection(name: str, dim: int) -> bool:
    key = f"{name}:{dim}"
    if _ready.get(key):
        return True
    try:
        from qdrant_client.models import Distance, VectorParams
        client = _get_client()
        if client is None:
            return False
        if not await client.collection_exists(name):
            await client.create_collection(
                name, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
        _ready[key] = True
        return True
    except Exception as e:
        logger.debug("ensure collection %s failed: %s", name, e)
        return False


async def _embed(texts: list[str]) -> list[list[float]] | None:
    try:
        return await gateway.embed(texts)
    except Exception as e:
        logger.debug("embed failed: %s", e)
        return None


async def remember_query(user_id: str, question: str, metric_ids: list[str],
                         sql_hash: str = "") -> None:
    """查询成功后写入语义记忆（异步任务调用，失败静默）。"""
    vecs = await _embed([question])
    if not vecs or not await _ensure_collection(COLL_QUERIES, len(vecs[0])):
        return
    try:
        from qdrant_client.models import PointStruct
        await _get_client().upsert(COLL_QUERIES, points=[PointStruct(
            id=str(uuid.uuid4()),
            vector=vecs[0],
            payload={"user_id": user_id, "question": question,
                     "metric_ids": metric_ids, "sql_hash": sql_hash},
        )])
    except Exception as e:
        logger.debug("remember_query failed: %s", e)


@dataclass
class SimilarQuery:
    question: str
    score: float
    metric_ids: list
    sql_hash: str


async def recall_similar_queries(user_id: str, question: str, limit: int = 3) -> list[SimilarQuery]:
    vecs = await _embed([question])
    if not vecs or not await _ensure_collection(COLL_QUERIES, len(vecs[0])):
        return []
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        res = await _get_client().search(
            COLL_QUERIES, query_vector=vecs[0], limit=limit,
            query_filter=Filter(should=[FieldCondition(key="user_id", match=MatchValue(value=user_id))]),
        )
        return [SimilarQuery(p.payload.get("question", ""), p.score,
                             p.payload.get("metric_ids", []), p.payload.get("sql_hash", ""))
                for p in res if p.score > 0.82]
    except Exception as e:
        logger.debug("recall failed: %s", e)
        return []


async def index_metric_texts(metrics: list) -> int:
    """指标文本入向量库（图谱变更后调用）。"""
    texts, ids = [], []
    for m in metrics:
        text = f"{m.name} {' '.join(m.aliases or [])} {m.business_def or ''}".strip()
        texts.append(text[:500])
        ids.append(str(m.id))
    if not texts:
        return 0
    vecs = await _embed(texts)
    if not vecs or not await _ensure_collection(COLL_METRIC_TEXTS, len(vecs[0])):
        return 0
    try:
        from qdrant_client.models import PointStruct
        await _get_client().upsert(COLL_METRIC_TEXTS, points=[
            PointStruct(id=i, vector=v, payload={"metric_id": i, "text": t})
            for i, v, t in zip(ids, vecs, texts)
        ])
        return len(ids)
    except Exception as e:
        logger.debug("index metrics failed: %s", e)
        return 0


async def search_metric_texts(question: str, top: int = 5) -> dict[str, float]:
    """向量召回指标候选：{metric_id: 相似度}。"""
    vecs = await _embed([question])
    if not vecs:
        return {}
    dim = len(vecs[0])
    if not await _ensure_collection(COLL_METRIC_TEXTS, dim):
        return {}
    try:
        res = await _get_client().search(COLL_METRIC_TEXTS, query_vector=vecs[0], limit=top)
        return {p.payload.get("metric_id", ""): p.score for p in res if p.score > 0.75}
    except Exception as e:
        logger.debug("metric vector search failed: %s", e)
        return {}


# ---------- PG 画像（无 embedding 也可用） ----------

async def get_profile(session, user_id) -> dict:
    row = (await session.execute(
        select(UserMemoryProfile).where(UserMemoryProfile.user_id == user_id)
    )).scalar_one_or_none()
    if row is None:
        return {"favorite_metrics": [], "habit_summary": ""}
    return {"favorite_metrics": row.favorite_metrics or [],
            "favorite_dimensions": row.favorite_dimensions or [],
            "habit_summary": row.habit_summary or ""}


async def update_profile_stats(session, user_id, metric_ids: list[str],
                               metric_names: dict[str, str], dims: list[str]) -> None:
    """查询成功后更新常用指标计数。user_id 接受 str / UUID。"""
    user_id = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    row = (await session.execute(
        select(UserMemoryProfile).where(UserMemoryProfile.user_id == user_id)
    )).scalar_one_or_none()
    if row is None:
        row = UserMemoryProfile(user_id=user_id)
        session.add(row)
    counts: dict[str, dict] = {m["metric_id"]: m for m in (row.favorite_metrics or [])}
    for mid in metric_ids:
        cur = counts.get(mid) or {"metric_id": mid, "name": metric_names.get(mid, mid), "count": 0}
        cur["count"] += 1
        counts[mid] = cur
    row.favorite_metrics = sorted(counts.values(), key=lambda x: -x["count"])[:20]
    dim_counts: dict[str, dict] = {d["name"]: d for d in (row.favorite_dimensions or [])}
    for d in dims:
        cur = dim_counts.get(d) or {"name": d, "count": 0}
        cur["count"] += 1
        dim_counts[d] = cur
    row.favorite_dimensions = sorted(dim_counts.values(), key=lambda x: -x["count"])[:10]


def favorite_metrics_prompt(profile: dict) -> str:
    favs = profile.get("favorite_metrics") or []
    if not favs:
        return ""
    names = "、".join(f["name"] for f in favs[:8] if f.get("name"))
    if not names:
        return ""
    out = f"用户常用指标: {names}"
    if profile.get("habit_summary"):
        out += f"\n用户分析习惯: {profile['habit_summary'][:300]}"
    return out
