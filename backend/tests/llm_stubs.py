"""LLM 协议测试替身：OpenAI / Anthropic 两个最小合规服务。

独立于业务代码，只做两件事：
1. 严格校验收到的请求是否符合对应协议契约（路径/鉴权头/system 位置/必填字段）；
2. 返回结构合法的响应（含流式），供适配器与网关解析。

契约违背一律返回 400 并在 `requests` 里留证，便于断言。
仅绑定 127.0.0.1 的临时端口。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def add(self, item: dict) -> None:
        self.requests.append(item)

    @property
    def last(self) -> dict | None:
        return self.requests[-1] if self.requests else None


class _BaseHandler(BaseHTTPRequestHandler):
    recorder: _Recorder
    reply_text = "pong"
    stream_chunks = 4

    def log_message(self, *args):
        pass

    # ---- 子类实现 ----
    def validate(self, body: dict) -> list[str]:
        return []

    def build_response(self, body: dict) -> dict:
        raise NotImplementedError

    def build_stream_events(self, body: dict) -> list[tuple[str, dict]]:
        raise NotImplementedError

    def auth_headers_expected(self) -> dict:
        return {}

    # ---- 通用流程 ----
    def _send(self, obj: dict, status: int = 200):
        payload = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode() if n else "{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._send({"error": {"message": "bad json"}}, 400)
            return

        problems = self.validate(body)
        self.recorder.add({
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
            "problems": problems,
        })
        if problems:
            self._send({"error": {"type": "invalid_request_error",
                                  "message": "; ".join(problems)}}, 400)
            return

        if body.get("stream"):
            self._stream(body)
            return
        self._send(self.build_response(body))

    def _stream(self, body: dict):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for name, obj in self.build_stream_events(body):
            if name:
                self.wfile.write(f"event: {name}\n".encode())
            self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()


def _text_of(body: dict) -> str:
    """取最后一条 user 文本，用于回显。"""
    for m in reversed(body.get("messages") or []):
        if m.get("role") == "user":
            c = m.get("content")
            return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
    return ""


class OpenAIHandler(_BaseHandler):
    """OpenAI /v1/chat/completions + /v1/embeddings。"""

    def validate(self, body: dict) -> list[str]:
        p = []
        if not self.path.endswith("/chat/completions") and not self.path.endswith("/embeddings"):
            p.append(f"路径不合法: {self.path}")
        h = {k.lower(): v for k, v in self.headers.items()}
        auth = h.get("authorization", "")
        if not auth.startswith("Bearer "):
            p.append("缺少 Authorization: Bearer 头")
        if "x-api-key" in h:
            p.append("OpenAI 协议不应出现 x-api-key 头")
        if "model" not in body:
            p.append("缺少 model")
        if "input" not in body and "messages" not in body:
            p.append("缺少 messages/input")
        return p

    def build_response(self, body: dict) -> dict:
        if self.path.endswith("/embeddings"):
            inputs = body.get("input") or []
            return {"object": "list", "data": [
                {"object": "embedding", "index": i, "embedding": [float(i), 0.5, 1.0]}
                for i in range(len(inputs))
            ], "usage": {"prompt_tokens": 3, "total_tokens": 3}}
        text = f"echo:{_text_of(body)}"
        return {
            "id": "cmpl-test", "object": "chat.completion", "model": body.get("model", ""),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 22},
        }

    def build_stream_events(self, body: dict) -> list[tuple[str, dict]]:
        text = f"echo:{_text_of(body)}"
        step = max(1, len(text) // self.stream_chunks)
        events: list[tuple[str, dict]] = []
        for i in range(0, len(text), step):
            events.append(("", {
                "id": "cmpl-test", "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": text[i:i + step]}}]}))
        events.append(("", {
            "id": "cmpl-test", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 22}}))
        events.append(("", {"__done__": True}))
        return events

    def _stream(self, body: dict):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.end_headers()
        for _, obj in self.build_stream_events(body):
            if obj.pop("__done__", False):
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()


class AnthropicHandler(_BaseHandler):
    """Anthropic /v1/messages。契约比 OpenAI 严：x-api-key、版本头、system 顶层。"""

    def validate(self, body: dict) -> list[str]:
        p = []
        if not self.path.endswith("/messages"):
            p.append(f"路径必须是 /v1/messages，收到 {self.path}")
        h = {k.lower(): v for k, v in self.headers.items()}
        if not h.get("x-api-key"):
            p.append("缺少 x-api-key 头")
        if h.get("authorization"):
            p.append("Anthropic 协议不应出现 Authorization 头")
        if h.get("anthropic-version") != "2023-06-01":
            p.append(f"anthropic-version 必须为 2023-06-01，收到 {h.get('anthropic-version')!r}")
        if any(m.get("role") == "system" for m in (body.get("messages") or [])):
            p.append("system 不得出现在 messages 中，必须放顶层 system 参数")
        if "max_tokens" not in body:
            p.append("max_tokens 必填")
        if "model" not in body:
            p.append("缺少 model")
        return p

    def build_response(self, body: dict) -> dict:
        text = f"echo:{_text_of(body)}"
        return {
            "id": "msg-test", "type": "message", "role": "assistant",
            "model": body.get("model", ""),
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 33, "output_tokens": 44},
        }

    def build_stream_events(self, body: dict) -> list[tuple[str, dict]]:
        text = f"echo:{_text_of(body)}"
        step = max(1, len(text) // self.stream_chunks)
        events = [("message_start", {"type": "message_start", "message": {
            "id": "msg-test", "type": "message", "role": "assistant",
            "content": [], "usage": {"input_tokens": 33, "output_tokens": 0}}})]
        events.append(("content_block_start", {"type": "content_block_start", "index": 0,
                                               "content_block": {"type": "text", "text": ""}}))
        for i in range(0, len(text), step):
            events.append(("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": text[i:i + step]}}))
        events.append(("content_block_stop", {"type": "content_block_stop", "index": 0}))
        events.append(("message_delta", {"type": "message_delta",
                                         "delta": {"stop_reason": "end_turn"},
                                         "usage": {"output_tokens": 44}}))
        events.append(("message_stop", {"type": "message_stop"}))
        return events


class StubServer:
    """把 handler 跑在后台线程，暴露 base_url 与请求记录。"""

    def __init__(self, handler_cls: type[_BaseHandler]):
        self.recorder = _Recorder()
        cls = type("BoundHandler", (handler_cls,), {"recorder": self.recorder})
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), cls)
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=3)
