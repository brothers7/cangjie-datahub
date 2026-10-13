"""Anthropic Messages 协议适配器：/v1/messages。

- 鉴权：x-api-key + anthropic-version: 2023-06-01
- system 消息必须放顶层 system 参数
- 流式事件：message_start / content_block_delta / message_delta / message_stop
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from app.llm.types import (BaseAdapter, ChatMessage, LLMAdapterError, LLMResponse, LLMUsage,
                           ResolvedConfig, StreamChunk)

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


def _url(cfg: ResolvedConfig) -> str:
    return cfg.endpoint.rstrip("/") + "/messages"


def _headers(cfg: ResolvedConfig) -> dict:
    return {
        "Content-Type": "application/json",
        "x-api-key": cfg.api_key,
        "anthropic-version": "2023-06-01",
    }


def _split_system(messages: list[ChatMessage]) -> tuple[str, list[dict]]:
    """Anthropic 要求 system 为顶层参数，不能出现在 messages 里。"""
    system_parts = [m.content for m in messages if m.role == "system"]
    chat = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
    return "\n\n".join(system_parts), chat


def _body(cfg: ResolvedConfig, messages: list[ChatMessage], *, stream: bool,
          temperature: float | None, max_tokens: int | None, top_p: float | None) -> dict:
    system, chat = _split_system(messages)
    body: dict = {
        "model": cfg.model,
        "messages": chat,
        "max_tokens": (cfg.max_tokens if max_tokens is None else max_tokens) or 4096,
        "temperature": cfg.temperature if temperature is None else temperature,
        "top_p": cfg.top_p if top_p is None else top_p,
        "stream": stream,
    }
    if system:
        body["system"] = system
    return body


def _parse_error(resp: httpx.Response) -> LLMAdapterError:
    brief = resp.text[:500]
    try:
        data = resp.json()
        msg = (data.get("error") or {}).get("message") or brief
    except Exception:
        msg = brief
    return LLMAdapterError(f"HTTP {resp.status_code}: {msg}",
                           retryable=resp.status_code in RETRYABLE_STATUS,
                           status=resp.status_code)


class AnthropicAdapter(BaseAdapter):
    async def complete(self, cfg: ResolvedConfig, messages: list[ChatMessage], *,
                       temperature: float | None = None, max_tokens: int | None = None,
                       top_p: float | None = None) -> LLMResponse:
        body = _body(cfg, messages, stream=False, temperature=temperature,
                     max_tokens=max_tokens, top_p=top_p)
        async with httpx.AsyncClient(timeout=cfg.timeout_sec) as client:
            resp = await client.post(_url(cfg), headers=_headers(cfg), json=body)
        if resp.status_code != 200:
            raise _parse_error(resp)
        data = resp.json()
        text = "".join(
            block.get("text", "") for block in data.get("content", [])
            if block.get("type") == "text"
        )
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            usage=LLMUsage(input_tokens=usage.get("input_tokens", 0),
                           output_tokens=usage.get("output_tokens", 0)),
            model=data.get("model", cfg.model),
            config_id=cfg.config_id,
        )

    async def stream(self, cfg: ResolvedConfig, messages: list[ChatMessage], *,  # type: ignore[override]
                     temperature: float | None = None, max_tokens: int | None = None,
                     top_p: float | None = None) -> AsyncIterator[StreamChunk]:
        body = _body(cfg, messages, stream=True, temperature=temperature,
                     max_tokens=max_tokens, top_p=top_p)
        usage = LLMUsage()
        async with httpx.AsyncClient(timeout=cfg.timeout_sec) as client:
            async with client.stream("POST", _url(cfg), headers=_headers(cfg), json=body) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    raise _parse_error(resp)
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    if etype == "message_start":
                        u = (event.get("message") or {}).get("usage") or {}
                        usage.input_tokens = u.get("input_tokens", 0)
                    elif etype == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield StreamChunk(delta=delta["text"])
                    elif etype == "message_delta":
                        u = event.get("usage") or {}
                        usage.output_tokens = u.get("output_tokens", 0)
                    elif etype == "error":
                        raise LLMAdapterError(str(event.get("error") or "stream error"))
        yield StreamChunk(usage=usage, done=True)
