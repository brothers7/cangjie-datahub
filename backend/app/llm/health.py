"""LLM 配置健康探测。"""
from __future__ import annotations

import time


from app.core.crypto import decrypt
from app.db.models.llm import LlmConfig, LlmHealthStatus, ModelKind
from app.db.session import get_session_factory
from app.llm.adapters.anthropic_adapter import AnthropicAdapter
from app.llm.adapters.openai_adapter import OpenAIAdapter
from app.llm.types import ChatMessage

_ADAPTERS = {"openai": OpenAIAdapter(), "anthropic": AnthropicAdapter()}


async def check_config(config_id: str) -> dict:
    """连通性测试：发一条最小请求。更新配置状态并返回结果。"""
    async with get_session_factory()() as session:
        row = await session.get(LlmConfig, config_id)
        if row is None:
            return {"ok": False, "message": "配置不存在"}
        adapter = _ADAPTERS[row.protocol.value]

        class _Cfg:  # 最小配置对象
            pass

        c = _Cfg()
        c.config_id = str(row.id)
        c.protocol = row.protocol.value
        c.kind = row.kind.value
        c.endpoint = row.endpoint
        c.api_key = decrypt(row.api_key_cipher, aad=f"llm:{row.id}")
        c.model = row.model
        c.temperature = 0.0
        c.max_tokens = 8
        c.top_p = 1.0
        c.timeout_sec = min(row.timeout_sec, 15)
        c.retry_count = 0
        c.stream_enabled = False

        t0 = time.monotonic()
        ok, msg, tokens = False, "", {}
        try:
            if row.kind == ModelKind.EMBEDDING:
                if row.protocol.value != "openai":
                    raise ValueError("embedding 仅支持 OpenAI 兼容协议")
                vecs = await adapter.embed(c, ["ping"])  # type: ignore[attr-defined]
                ok, tokens = True, {"dim": len(vecs[0]) if vecs else 0}
            else:
                resp = await adapter.complete(c, [ChatMessage(role="user", content="ping")])  # type: ignore[attr-defined]
                ok = True
                tokens = {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens}
        except Exception as e:
            msg = str(e)[:300]

        latency_ms = int((time.monotonic() - t0) * 1000)
        row.status = LlmHealthStatus.HEALTHY if ok else LlmHealthStatus.ERROR
        row.status_message = "ok" if ok else msg
        row.health_latency_ms = latency_ms
        from app.db.models.base import utcnow as _utcnow
        row.last_health_at = _utcnow()
        await session.commit()
        return {"ok": ok, "message": "ok" if ok else msg,
                "latency_ms": latency_ms, "tokens": tokens}
