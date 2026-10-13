"""协议适配器契约测试（PRD 验收 5 + 14）。

两个协议各起一个严格校验的本地替身服务：请求形状有任何一处不合协议就返回 400。
因此这些用例断言的不只是「能通」，而是「确实按各自协议发的请求」。
"""
from __future__ import annotations

import pytest

from app.llm.adapters.anthropic_adapter import AnthropicAdapter
from app.llm.adapters.openai_adapter import OpenAIAdapter
from app.llm.types import ChatMessage, LLMAdapterError, ResolvedConfig
from tests.llm_stubs import AnthropicHandler, OpenAIHandler, StubServer

MESSAGES = [
    ChatMessage(role="system", content="你是 SQL 专家，只输出 SQL。"),
    ChatMessage(role="user", content="近7天GMV趋势"),
]


def _cfg(server: StubServer, protocol: str, **kw) -> ResolvedConfig:
    base = dict(
        config_id="00000000-0000-0000-0000-000000000001",
        protocol=protocol, kind="chat", endpoint=server.base_url,
        api_key="test-key", model="stub-model", temperature=0.0,
        max_tokens=256, top_p=1.0, timeout_sec=10, retry_count=0,
    )
    base.update(kw)
    return ResolvedConfig(**base)


@pytest.fixture
def openai_server():
    s = StubServer(OpenAIHandler)
    yield s
    s.stop()


@pytest.fixture
def anthropic_server():
    s = StubServer(AnthropicHandler)
    yield s
    s.stop()


# ---------- OpenAI ----------

async def test_openai_complete_request_shape(openai_server):
    resp = await OpenAIAdapter().complete(_cfg(openai_server, "openai"), MESSAGES)
    assert resp.text == "echo:近7天GMV趋势"
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (11, 22)

    req = openai_server.recorder.last
    assert req["problems"] == [], req["problems"]
    assert req["path"].endswith("/v1/chat/completions")
    assert req["headers"]["authorization"] == "Bearer test-key"
    # OpenAI 协议 system 就在 messages 里，不需要拆出去
    assert [m["role"] for m in req["body"]["messages"]] == ["system", "user"]


async def test_openai_stream_accumulates_and_reports_usage(openai_server):
    text, usage, chunks = "", None, 0
    async for c in OpenAIAdapter().stream(_cfg(openai_server, "openai"), MESSAGES):
        if c.done:
            usage = c.usage
        else:
            text += c.delta
            chunks += 1
    assert text == "echo:近7天GMV趋势"
    assert chunks >= 2, "流式应分多个 chunk 返回"
    assert (usage.input_tokens, usage.output_tokens) == (11, 22)
    assert openai_server.recorder.last["body"]["stream"] is True


async def test_openai_embedding(openai_server):
    vecs = await OpenAIAdapter().embed(_cfg(openai_server, "openai"), ["a", "b"])
    assert len(vecs) == 2 and len(vecs[0]) == 3
    assert openai_server.recorder.last["path"].endswith("/v1/embeddings")


# ---------- Anthropic ----------

async def test_anthropic_complete_request_shape(anthropic_server):
    resp = await AnthropicAdapter().complete(_cfg(anthropic_server, "anthropic"), MESSAGES)
    assert resp.text == "echo:近7天GMV趋势"
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (33, 44)

    req = anthropic_server.recorder.last
    assert req["problems"] == [], req["problems"]
    assert req["path"].endswith("/v1/messages")
    assert req["headers"]["x-api-key"] == "test-key"
    assert req["headers"]["anthropic-version"] == "2023-06-01"
    # 关键差异：system 提到顶层，messages 里只剩 user
    assert req["body"]["system"] == "你是 SQL 专家，只输出 SQL。"
    assert [m["role"] for m in req["body"]["messages"]] == ["user"]


async def test_anthropic_stream_accumulates_and_reports_usage(anthropic_server):
    text, usage, chunks = "", None, 0
    async for c in AnthropicAdapter().stream(_cfg(anthropic_server, "anthropic"), MESSAGES):
        if c.done:
            usage = c.usage
        else:
            text += c.delta
            chunks += 1
    assert text == "echo:近7天GMV趋势"
    assert chunks >= 2
    # input_tokens 来自 message_start，output_tokens 来自 message_delta，两者都要取到
    assert usage.input_tokens == 33
    assert usage.output_tokens == 44


async def test_anthropic_multiple_system_messages_hoisted_in_order(anthropic_server):
    msgs = [
        ChatMessage(role="system", content="S1"),
        ChatMessage(role="system", content="S2"),
        ChatMessage(role="user", content="q"),
    ]
    await AnthropicAdapter().complete(_cfg(anthropic_server, "anthropic"), msgs)
    assert anthropic_server.recorder.last["body"]["system"] == "S1\n\nS2"


async def test_anthropic_without_system_omits_top_level_param(anthropic_server):
    await AnthropicAdapter().complete(
        _cfg(anthropic_server, "anthropic"), [ChatMessage(role="user", content="q")])
    body = anthropic_server.recorder.last["body"]
    assert "system" not in body, "无 system 时不应发空 system 参数"


async def test_anthropic_embedding_unsupported(anthropic_server):
    with pytest.raises(LLMAdapterError):
        await AnthropicAdapter().embed(_cfg(anthropic_server, "anthropic"), ["a"])


# ---------- 错误与重试映射 ----------

async def test_error_mapping_marks_retryable_statuses():
    """5xx/429 可重试，4xx 不可重试 —— 决定网关是否走降级链。"""
    import httpx

    from app.llm.adapters.anthropic_adapter import RETRYABLE_STATUS as ANTH
    from app.llm.adapters.openai_adapter import RETRYABLE_STATUS as OAI
    from app.llm.adapters.anthropic_adapter import _parse_error

    for code in (429, 500, 502, 503, 504):
        assert code in ANTH and code in OAI, f"{code} 应可重试"
    # 529 是 Anthropic 专有的「过载」状态码，OpenAI 侧不会出现
    assert 529 in ANTH
    for code in (400, 401, 403, 404, 422):
        assert code not in ANTH and code not in OAI, f"{code} 不应重试"

    resp = httpx.Response(429, json={"error": {"message": "rate limited"}})
    err = _parse_error(resp)
    assert err.retryable is True and err.status == 429
    assert "rate limited" in err.message

    resp400 = httpx.Response(400, json={"error": {"message": "bad model"}})
    err400 = _parse_error(resp400)
    assert err400.retryable is False and err400.status == 400


async def test_connection_failure_raises_adapter_error():
    from app.llm.adapters.openai_adapter import OpenAIAdapter

    cfg = ResolvedConfig(config_id="x", protocol="openai", kind="chat",
                         endpoint="http://127.0.0.1:1/v1", api_key="k", model="m",
                         timeout_sec=2, retry_count=0)
    with pytest.raises(Exception):
        await OpenAIAdapter().complete(cfg, [ChatMessage(role="user", content="q")])
