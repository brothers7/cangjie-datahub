"""LLM 统一网关：全项目所有模型调用的唯一入口。

职责：场景路由（scene→config）→ 协议适配（openai/anthropic）→ 超时重试 →
服务商故障自动降级（fallback 配置链）→ 调用日志与 token 统计。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.core.crypto import decrypt
from app.core.errors import LLMError
from app.db.models.llm import CallStatus, LlmCallLog, LlmConfig, LlmScene, LlmSceneBinding, ModelKind, Protocol
from app.db.session import get_session_factory
from app.llm.adapters.anthropic_adapter import AnthropicAdapter
from app.llm.adapters.openai_adapter import OpenAIAdapter
from app.llm.types import ChatMessage, LLMAdapterError, LLMResponse, LLMUsage, ResolvedConfig, StreamChunk

logger = logging.getLogger(__name__)

ADAPTERS = {
    Protocol.OPENAI: OpenAIAdapter(),
    Protocol.ANTHROPIC: AnthropicAdapter(),
}

_CONFIG_CACHE_TTL = 15.0  # 秒：模型切换无需重启即生效的上限延迟


class _TTLCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str) -> object | None:
        hit = self._store.get(key)
        if not hit:
            return None
        expire, val = hit
        if time.monotonic() > expire:
            self._store.pop(key, None)
            return None
        return val

    def set(self, key: str, val: object, ttl: float = _CONFIG_CACHE_TTL) -> None:
        self._store[key] = (time.monotonic() + ttl, val)

    def invalidate(self, prefix: str = "") -> None:
        if not prefix:
            self._store.clear()
        else:
            for k in [k for k in self._store if k.startswith(prefix)]:
                self._store.pop(k, None)


cache = _TTLCache()

# 失败告警节流：同一配置 5 分钟内最多发 1 次 webhook（进程内记录）
ALERT_THROTTLE_SEC = 300.0
_ALERT_LAST_SENT: dict[str, float] = {}


def invalidate_config_cache() -> None:
    """配置变更后由 API 层调用，立即生效。"""
    cache.invalidate()


class LLMGateway:
    # ---------- 配置解析 ----------
    async def _load_row(self, config_id: str) -> LlmConfig | None:
        async with get_session_factory()() as session:
            return await session.get(LlmConfig, config_id)

    def _to_resolved(self, row: LlmConfig) -> ResolvedConfig:
        return ResolvedConfig(
            config_id=str(row.id),
            protocol=row.protocol.value,
            kind=row.kind.value,
            endpoint=row.endpoint,
            api_key=decrypt(row.api_key_cipher, aad=f"llm:{row.id}"),
            model=row.model,
            temperature=row.temperature,
            max_tokens=row.max_tokens,
            top_p=row.top_p,
            timeout_sec=row.timeout_sec,
            retry_count=row.retry_count,
            stream_enabled=row.stream_enabled,
            name=row.name,
            fallback_config_id=str(row.fallback_config_id) if row.fallback_config_id else None,
        )

    async def resolve_scene(self, scene: LlmScene | str, *, kind: ModelKind = ModelKind.CHAT,
                            override_config_id: str | None = None,
                            conversation_id: str | None = None) -> ResolvedConfig:
        """解析某场景应使用的模型配置。
        override_config_id：会话级覆盖（用户在对话里手动选了大模型），优先于场景绑定与全局默认。
        conversation_id：new_session 切换策略接线——会话已锁定快照且快照配置仍为
        new_session 时沿用快照；否则按当前默认/绑定解析，命中 new_session 配置则落快照。
        """
        scene_key = scene.value if isinstance(scene, LlmScene) else str(scene)
        cache_key = f"scene:{kind.value}:{scene_key}:{override_config_id or ''}:{conversation_id or ''}"
        cached = cache.get(cache_key)
        if cached:
            return cached  # type: ignore[return-value]

        async with get_session_factory()() as session:
            config_row: LlmConfig | None = None
            if override_config_id:
                try:
                    config_row = await session.get(LlmConfig, uuid.UUID(override_config_id))
                    if config_row is not None and config_row.kind != kind:
                        config_row = None   # 覆盖配置类型不符（如对 embedding 场景给了 chat 配置）则忽略
                except (ValueError, AttributeError):
                    config_row = None
            if config_row is None and kind == ModelKind.CHAT:
                binding = (await session.execute(
                    select(LlmSceneBinding).where(LlmSceneBinding.scene == scene_key)
                )).scalar_one_or_none()
                if binding:
                    config_row = await session.get(LlmConfig, binding.config_id)
            if config_row is None:
                default_flag = (LlmConfig.is_default_embedding if kind == ModelKind.EMBEDDING
                                else LlmConfig.is_default)
                config_row = (await session.execute(
                    select(LlmConfig)
                    .where(default_flag.is_(True), LlmConfig.kind == kind)
                    .order_by(LlmConfig.created_at)
                    .limit(1)
                )).scalar_one_or_none()
            if (conversation_id and kind == ModelKind.CHAT and not override_config_id
                    and config_row is not None):
                config_row = await self._apply_session_snapshot(
                    session, conversation_id, config_row)
        if config_row is None:
            raise LLMError(
                "尚未配置 LLM 模型，请管理员在「LLM 配置」页添加"
                + ("（含 embedding 模型）" if kind == ModelKind.EMBEDDING else "")
            )
        resolved = self._to_resolved(config_row)
        cache.set(cache_key, resolved)
        return resolved

    async def _apply_session_snapshot(self, session, conversation_id: str,
                                      config_row: LlmConfig) -> LlmConfig:
        """new_session 切换策略：进行中的旧会话继续走旧配置，新会话用新配置。

        - 会话已有快照且快照配置仍为 new_session → 返回快照配置（默认/绑定变更不影响）；
        - 快照配置被删或策略改回 immediate → 清快照，按当前解析结果走；
        - 无快照且当前解析命中 new_session 配置 → 落快照（首次解析即锁定）。
        """
        from app.db.models.chat import Conversation  # 延迟导入避免循环依赖
        try:
            conv = await session.get(Conversation, uuid.UUID(str(conversation_id)))
        except (ValueError, AttributeError):
            return config_row
        if conv is None:
            return config_row
        dirty = False
        snap_id = getattr(conv, "llm_snapshot_config_id", None)
        if snap_id:
            snap = await session.get(LlmConfig, snap_id)
            if snap is not None and snap.switch_policy == "new_session":
                return snap  # 旧会话沿用快照配置
            conv.llm_snapshot_config_id = None  # 快照失效：配置删除或策略已改回即时生效
            dirty = True
        if config_row.switch_policy == "new_session":
            conv.llm_snapshot_config_id = config_row.id
            dirty = True
        if dirty:
            try:
                await session.commit()  # 快照持久化失败不阻断解析
            except Exception:
                logger.warning("会话 LLM 快照写入失败（不影响本次解析）", exc_info=True)
        return config_row

    # ---------- 调用（含重试与降级） ----------
    async def _call_with_retry(self, cfg: ResolvedConfig, adapter, messages, **kw) -> LLMResponse:
        attempts = max(0, cfg.retry_count) + 1
        last_err: Exception | None = None
        for i in range(attempts):
            try:
                return await adapter.complete(cfg, messages, **kw)
            except LLMAdapterError as e:
                last_err = e
                if not e.retryable or i == attempts - 1:
                    raise
            except httpx.HTTPError as e:  # 网络/超时
                last_err = LLMAdapterError(f"网络错误: {e.__class__.__name__}", retryable=True)
                if i == attempts - 1:
                    raise last_err
            await asyncio.sleep(min(2.0, 0.5 * (i + 1)))
        raise last_err or LLMAdapterError("未知错误")

    async def _resolve_chain(self, cfg: ResolvedConfig) -> list[ResolvedConfig]:
        """主配置 + 降级链（防环，最多 3 层）。"""
        chain = [cfg]
        cur = cfg
        seen = {cfg.config_id}
        while cur.fallback_config_id and len(chain) < 3:
            fid = cur.fallback_config_id
            if fid in seen:
                break
            row = await self._load_row(fid)
            if row is None:
                break
            cur = self._to_resolved(row)
            seen.add(fid)
            chain.append(cur)
        return chain

    async def chat(self, scene: LlmScene | str, messages: list[ChatMessage], *,
                   conversation_id: str | None = None,
                   override_config_id: str | None = None,
                   temperature: float | None = None, max_tokens: int | None = None,
                   top_p: float | None = None) -> LLMResponse:
        cfg = await self.resolve_scene(scene, override_config_id=override_config_id,
                                       conversation_id=conversation_id)
        chain = await self._resolve_chain(cfg)
        primary_err: Exception | None = None
        for idx, c in enumerate(chain):
            adapter = ADAPTERS[Protocol(c.protocol)]
            t0 = time.monotonic()
            try:
                resp = await self._call_with_retry(
                    c, adapter, messages,
                    temperature=temperature, max_tokens=max_tokens, top_p=top_p,
                )
                resp.degraded = idx > 0
                await self._log(c, scene, resp.usage, time.monotonic() - t0,
                                CallStatus.OK if idx == 0 else CallStatus.DEGRADED,
                                "", conversation_id, primary_err)
                return resp
            except Exception as e:
                primary_err = primary_err or e
                await self._log(c, scene, LLMUsage(), time.monotonic() - t0,
                                CallStatus.ERROR, str(e)[:500], conversation_id, None)
                if idx == len(chain) - 1:
                    await self._maybe_alert(c, scene, e)  # 重试+降级后仍失败：主动告警
                    raise LLMError(f"LLM 调用失败（{c.name}）: {e}") from e
        raise LLMError("LLM 调用失败")

    async def chat_stream(self, scene: LlmScene | str, messages: list[ChatMessage], *,
                          conversation_id: str | None = None,
                          override_config_id: str | None = None,
                          temperature: float | None = None, max_tokens: int | None = None,
                          top_p: float | None = None) -> AsyncIterator[StreamChunk]:
        """流式调用。降级仅在「首个增量产出前失败」时启用，避免重复输出。"""
        cfg = await self.resolve_scene(scene, override_config_id=override_config_id,
                                       conversation_id=conversation_id)
        chain = await self._resolve_chain(cfg)
        primary_err: Exception | None = None

        for idx, c in enumerate(chain):
            adapter = ADAPTERS[Protocol(c.protocol)]
            t0 = time.monotonic()
            usage = LLMUsage()
            produced = False
            try:
                inner = adapter.stream(c, messages, temperature=temperature,
                                       max_tokens=max_tokens, top_p=top_p)
                async for chunk in inner:
                    if chunk.delta:
                        produced = True
                    if chunk.usage:
                        usage = chunk.usage
                    yield chunk
                await self._log(c, scene, usage, time.monotonic() - t0,
                                CallStatus.OK if idx == 0 else CallStatus.DEGRADED,
                                "", conversation_id, primary_err)
                return
            except Exception as e:
                await self._log(c, scene, usage, time.monotonic() - t0,
                                CallStatus.ERROR, str(e)[:500], conversation_id, None)
                if produced or idx == len(chain) - 1:
                    await self._maybe_alert(c, scene, e)  # 彻底失败：主动告警
                    raise LLMError(f"LLM 流式调用失败（{c.name}）: {e}") from e
                primary_err = primary_err or e
        raise LLMError("LLM 流式调用失败")

    async def embed(self, texts: list[str], *, scene: LlmScene | str = LlmScene.EMBED_QUERY) -> list[list[float]]:
        cfg = await self.resolve_scene(scene, kind=ModelKind.EMBEDDING)
        adapter = ADAPTERS.get(Protocol(cfg.protocol))
        if adapter is None:
            raise LLMError("该协议不支持 embedding")
        t0 = time.monotonic()
        try:
            vectors = await adapter.embed(cfg, texts)
            await self._log(cfg, scene, LLMUsage(input_tokens=sum(len(t) for t in texts) // 2),
                            time.monotonic() - t0, CallStatus.OK, "", None, None)
            return vectors
        except Exception as e:
            await self._log(cfg, scene, LLMUsage(), time.monotonic() - t0,
                            CallStatus.ERROR, str(e)[:500], None, None)
            await self._maybe_alert(cfg, scene, e)  # 彻底失败：主动告警
            raise LLMError(f"embedding 调用失败: {e}") from e

    async def embedding_available(self) -> bool:
        try:
            await self.resolve_scene(LlmScene.EMBED_QUERY, kind=ModelKind.EMBEDDING)
            return True
        except LLMError:
            return False

    # ---------- 失败告警 ----------
    async def _maybe_alert(self, cfg: ResolvedConfig, scene: LlmScene | str, error: Exception) -> None:
        """LLM 彻底失败（重试 + 降级链后仍失败）时向 webhook 主动告警。

        未配置 settings.alert_webhook_url 时直接跳过；同一配置 5 分钟内最多 1 次；
        webhook 发送失败只记日志，绝不影响主调用链。
        """
        try:
            url = getattr(settings, "alert_webhook_url", "") or ""
            if not url:
                return
            now = time.monotonic()
            if now - _ALERT_LAST_SENT.get(cfg.config_id, -1e12) < ALERT_THROTTLE_SEC:
                return  # 节流：同配置 5 分钟内不重复告警
            _ALERT_LAST_SENT[cfg.config_id] = now
            payload = {
                "type": "llm_failure",
                "config_name": cfg.name,
                "config_id": cfg.config_id,
                "scene": scene.value if isinstance(scene, LlmScene) else str(scene),
                "model": cfg.model,
                "error": str(error)[:500],
                "time": datetime.now(UTC).isoformat(),
            }
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(url, json=payload)
        except Exception:
            logger.warning("LLM 失败告警 webhook 发送失败（不影响主链路）", exc_info=True)

    # ---------- 日志 ----------
    async def _log(self, cfg: ResolvedConfig, scene: LlmScene | str, usage: LLMUsage,
                   elapsed: float, status: CallStatus, error: str,
                   conversation_id: str | None, degraded_from: Exception | None) -> None:
        try:
            async with get_session_factory()() as session:
                session.add(LlmCallLog(
                    config_id=cfg.config_id,
                    scene=scene.value if isinstance(scene, LlmScene) else str(scene),
                    protocol=cfg.protocol,
                    model=cfg.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    latency_ms=int(elapsed * 1000),
                    status=status,
                    error_brief=(error + (f" | 降级自: {degraded_from}" if degraded_from else ""))[:1000],
                    conversation_id=conversation_id,
                ))
                await session.commit()
        except Exception:
            # 日志失败不能影响主链路，但也绝不能静默：
            # 静默吞掉会直接导致 PRD 5.14 的「LLM 调用日志 + token 统计」整块失效而不自知。
            logger.warning("LLM 调用日志写入失败（不影响主链路）", exc_info=True)


gateway = LLMGateway()
