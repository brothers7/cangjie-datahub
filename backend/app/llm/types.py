"""LLM 网关统一类型。"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal

Role = Literal["system", "user", "assistant"]


@dataclass
class ChatMessage:
    role: Role
    content: str


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: "LLMUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens


@dataclass
class LLMResponse:
    text: str = ""
    usage: LLMUsage = field(default_factory=LLMUsage)
    model: str = ""
    config_id: str | None = None
    degraded: bool = False  # 是否走了降级配置


@dataclass
class StreamChunk:
    """流式产出：delta 为增量文本；done=True 时 usage 有效。"""
    delta: str = ""
    usage: LLMUsage | None = None
    done: bool = False


@dataclass
class ResolvedConfig:
    """解密后可直接调用的模型配置。"""
    config_id: str
    protocol: str            # openai / anthropic
    kind: str                # chat / embedding
    endpoint: str
    api_key: str
    model: str
    temperature: float = 0.2
    max_tokens: int = 4096
    top_p: float = 1.0
    timeout_sec: int = 30
    retry_count: int = 2
    stream_enabled: bool = True
    name: str = ""
    fallback_config_id: str | None = None


class LLMAdapterError(Exception):
    """适配器调用失败（网络/协议/鉴权）。"""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status = status


class BaseAdapter:
    """协议适配器抽象。"""

    async def complete(self, cfg: ResolvedConfig, messages: list[ChatMessage],
                       *, temperature: float | None = None, max_tokens: int | None = None,
                       top_p: float | None = None) -> LLMResponse:
        raise NotImplementedError

    def stream(self, cfg: ResolvedConfig, messages: list[ChatMessage], *,
               temperature: float | None = None, max_tokens: int | None = None,
               top_p: float | None = None) -> AsyncIterator[StreamChunk]:
        raise NotImplementedError

    async def embed(self, cfg: ResolvedConfig, texts: list[str]) -> list[list[float]]:
        raise LLMAdapterError("该协议不支持 embedding 接口")

    async def health(self, cfg: ResolvedConfig) -> LLMResponse:
        return await self.complete(
            cfg, [ChatMessage(role="user", content="ping")], max_tokens=8
        )
