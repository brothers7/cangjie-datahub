"""LLM 协议适配器包。

共享 httpx.AsyncClient：按 (endpoint, 事件循环) 键控复用。
原先每次调用都新建 AsyncClient，TCP/TLS 连接无法复用，高并发问答/流式场景
每次请求都付出一次握手开销；连接池也无法发挥作用。

键控说明：
- endpoint 在键里 —— 不会跨配置复用到错误地址（鉴权头等仍在每次请求上单独带）；
- 事件循环在键里 —— httpx 连接池绑定创建时所在的 loop，跨 loop 复用
  （如测试每个用例一个新 loop）会抛 "Event loop is closed"，必须按 loop 隔离；
- client 已关闭时自动重建，失效策略从简。
"""
from __future__ import annotations

import asyncio

import httpx

_clients: dict[tuple[str, asyncio.AbstractEventLoop], httpx.AsyncClient] = {}


def shared_client(endpoint: str) -> httpx.AsyncClient:
    """取 endpoint 对应的共享 AsyncClient（不存在或已关闭则新建）。"""
    key = (endpoint.rstrip("/"), asyncio.get_running_loop())
    cli = _clients.get(key)
    if cli is None or cli.is_closed:
        cli = httpx.AsyncClient()
        _clients[key] = cli
    return cli


async def close_shared_clients() -> None:
    """关闭全部共享 client（进程退出/测试清理用）。"""
    for cli in _clients.values():
        try:
            await cli.aclose()
        except Exception:
            pass
    _clients.clear()
