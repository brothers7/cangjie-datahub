"""联网检索（Tavily）：行业对标 / 政策参考。外部数据仅作分析参考，绝不覆盖内部数仓数据。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

API_URL = "https://api.tavily.com/search"


@dataclass
class WebEvidence:
    title: str
    url: str
    snippet: str


@dataclass
class WebSearchResult:
    ok: bool = False
    query: str = ""
    evidences: list[WebEvidence] = field(default_factory=list)
    error: str = ""


def available() -> bool:
    return bool(settings.tavily_api_key)


async def search(query: str, max_results: int = 5) -> WebSearchResult:
    if not available():
        return WebSearchResult(ok=False, query=query,
                               error="未配置 TAVILY_API_KEY，联网检索未启用")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(API_URL, json={
                "api_key": settings.tavily_api_key,
                "query": query,
                "max_results": max_results,
                "include_answer": False,
            })
        if resp.status_code != 200:
            return WebSearchResult(ok=False, query=query, error=f"HTTP {resp.status_code}")
        data = resp.json()
        evs = [WebEvidence(r.get("title", ""), r.get("url", ""), r.get("content", "")[:400])
               for r in data.get("results", [])]
        return WebSearchResult(ok=True, query=query, evidences=evs)
    except Exception as e:
        logger.info("tavily search failed: %s", e)
        return WebSearchResult(ok=False, query=query, error=str(e)[:200])


def evidences_to_text(result: WebSearchResult, limit: int = 4) -> str:
    lines = []
    for e in result.evidences[:limit]:
        lines.append(f"- [{e.title}]({e.url})：{e.snippet}")
    return "\n".join(lines)
