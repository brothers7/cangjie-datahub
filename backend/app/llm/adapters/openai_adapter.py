"""OpenAI 兼容协议适配器：/v1/chat/completions、/v1/embeddings。

兼容 OpenAI 官方及所有兼容端点（DeepSeek、GLM、Kimi、vLLM、Ollama 等），
endpoint 填到 /v1 为止，例如 https://api.openai.com/v1。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from app.llm.adapters import shared_client
from app.llm.types import (BaseAdapter, ChatMessage, LLMAdapterError, LLMResponse, LLMUsage,
                           ResolvedConfig, StreamChunk)

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def _url(cfg: ResolvedConfig, path: str) -> str:
    return cfg.endpoint.rstrip("/") + path


def _headers(cfg: ResolvedConfig) -> dict:
    h = {"Content-Type": "application/json"}
    if cfg.api_key:
        h["Authorization"] = f"Bearer {cfg.api_key}"
    return h


def _body(cfg: ResolvedConfig, messages: list[ChatMessage], *, stream: bool,
          temperature: float | None, max_tokens: int | None, top_p: float | None) -> dict:
    body: dict = {
        "model": cfg.model,
        "messages": [{"role": m.role, "content": m.content} for m in messages],
        "temperature": cfg.temperature if temperature is None else temperature,
        "top_p": cfg.top_p if top_p is None else top_p,
        "stream": stream,
    }
    # max_tokens：为 0 表示不限制
    mt = cfg.max_tokens if max_tokens is None else max_tokens
    if mt and mt > 0:
        body["max_tokens"] = mt
    if stream:
        body["stream_options"] = {"include_usage": True}
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


class OpenAIAdapter(BaseAdapter):
    async def complete(self, cfg: ResolvedConfig, messages: list[ChatMessage], *,
                       temperature: float | None = None, max_tokens: int | None = None,
                       top_p: float | None = None) -> LLMResponse:
        body = _body(cfg, messages, stream=False, temperature=temperature,
                     max_tokens=max_tokens, top_p=top_p)
        # 共享 client（按 endpoint 键控）复用连接；超时仍按配置逐请求传入
        resp = await shared_client(cfg.endpoint).post(
            _url(cfg, "/chat/completions"), headers=_headers(cfg), json=body,
            timeout=cfg.timeout_sec)
        if resp.status_code != 200:
            raise _parse_error(resp)
        data = resp.json()
        choices = data.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content") or ""
        usage = (data.get("usage") or {})
        return LLMResponse(
            text=text,
            usage=LLMUsage(input_tokens=usage.get("prompt_tokens", 0),
                           output_tokens=usage.get("completion_tokens", 0)),
            model=data.get("model", cfg.model),
            config_id=cfg.config_id,
        )

    async def stream(self, cfg: ResolvedConfig, messages: list[ChatMessage], *,  # type: ignore[override]
                     temperature: float | None = None, max_tokens: int | None = None,
                     top_p: float | None = None) -> AsyncIterator[StreamChunk]:
        body = _body(cfg, messages, stream=True, temperature=temperature,
                     max_tokens=max_tokens, top_p=top_p)
        usage = LLMUsage()
        async with shared_client(cfg.endpoint).stream(
                "POST", _url(cfg, "/chat/completions"),
                headers=_headers(cfg), json=body, timeout=cfg.timeout_sec) as resp:
            if resp.status_code != 200:
                await resp.aread()
                raise _parse_error(resp)
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                u = chunk.get("usage")
                if u:
                    usage = LLMUsage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
                for choice in chunk.get("choices") or []:
                    delta = (choice.get("delta") or {}).get("content")
                    if delta:
                        yield StreamChunk(delta=delta)
        yield StreamChunk(usage=usage, done=True)

    async def embed(self, cfg: ResolvedConfig, texts: list[str]) -> list[list[float]]:
        resp = await shared_client(cfg.endpoint).post(
            _url(cfg, "/embeddings"), headers=_headers(cfg),
            json={"model": cfg.model, "input": texts}, timeout=cfg.timeout_sec)
        if resp.status_code != 200:
            raise _parse_error(resp)
        data = resp.json().get("data") or []
        # 按 index 排序保证与输入顺序一致
        data = sorted(data, key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data]
