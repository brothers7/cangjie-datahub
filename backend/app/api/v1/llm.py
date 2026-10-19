"""LLM 配置管理：CRUD / 健康检查 / 激活 / 场景绑定 / 用量统计 / 调用日志。"""
from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.crypto import encrypt
from app.core.errors import NotFoundError, ValidationError
from app.db.models import AuditLog, LlmCallLog, LlmConfig, LlmScene, LlmSceneBinding, ModelKind, Protocol
from app.db.models.base import utcnow
from app.db.models.llm import CallStatus
from app.db.session import get_db
from app.llm.gateway import invalidate_config_cache
from app.llm.health import check_config
from app.schemas.entities import LlmConfigIn, LlmConfigOut, LlmConfigUpdateIn

router = APIRouter(prefix="/llm", tags=["llm"])

SCENES = [s.value for s in LlmScene]


@router.get("/configs", response_model=list[LlmConfigOut])
async def list_configs(db: AsyncSession = Depends(get_db), _=Depends(require_admin)):
    rows = (await db.execute(select(LlmConfig).order_by(LlmConfig.created_at))).scalars().all()
    return rows


@router.get("/chat-options")
async def chat_options(db: AsyncSession = Depends(get_db), _=Depends(get_current_user)):
    """对话窗口可选的对话模型列表（登录即可，不含密钥等敏感字段）。"""
    rows = (await db.execute(select(LlmConfig).where(LlmConfig.kind == ModelKind.CHAT)
                             .order_by(LlmConfig.created_at))).scalars().all()
    return [{"id": str(r.id), "name": r.name, "model": r.model,
             "protocol": r.protocol.value, "is_default": r.is_default,
             "health_status": r.status.value} for r in rows]


@router.post("/configs", response_model=LlmConfigOut)
async def create_config(body: LlmConfigIn, db: AsyncSession = Depends(get_db),
                        user=Depends(require_admin)):
    if body.protocol not in ("openai", "anthropic"):
        raise ValidationError("protocol 必须为 openai 或 anthropic")
    if body.kind == "embedding" and body.protocol != "openai":
        raise ValidationError("embedding 仅支持 OpenAI 兼容协议")
    exists = (await db.execute(select(LlmConfig).where(LlmConfig.name == body.name))).scalar_one_or_none()
    if exists:
        raise ValidationError("配置名已存在")
    cfg = LlmConfig(name=body.name, protocol=Protocol(body.protocol),
                    kind=ModelKind(body.kind), endpoint=body.endpoint,
                    model=body.model, temperature=body.temperature,
                    max_tokens=body.max_tokens, top_p=body.top_p,
                    timeout_sec=body.timeout_sec, retry_count=body.retry_count,
                    stream_enabled=body.stream_enabled,
                    fallback_config_id=uuid.UUID(body.fallback_config_id) if body.fallback_config_id else None,
                    switch_policy=body.switch_policy)
    db.add(cfg)
    await db.flush()
    cfg.api_key_cipher = encrypt(body.api_key, aad=f"llm:{cfg.id}") if body.api_key else ""
    await _set_default_flags(db, cfg, body.is_default, body.is_default_embedding)
    db.add(AuditLog(user_id=user.id, username=user.username, action="llm.create",
                    resource_type="llm_config", resource_id=str(cfg.id),
                    detail={"name": body.name, "protocol": body.protocol, "model": body.model}))
    await db.commit()
    await db.refresh(cfg)
    invalidate_config_cache()  # 新建配置（尤其设为默认）立即生效
    return cfg


@router.patch("/configs/{config_id}", response_model=LlmConfigOut)
async def update_config(config_id: str, body: LlmConfigUpdateIn,
                        db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    cfg = await db.get(LlmConfig, uuid.UUID(config_id))
    if cfg is None:
        raise NotFoundError("配置不存在")
    simple = ["name", "endpoint", "model", "temperature", "max_tokens", "top_p",
              "timeout_sec", "retry_count", "stream_enabled", "switch_policy"]
    for f in simple:
        v = getattr(body, f)
        if v is not None:
            setattr(cfg, f, v)
    if body.api_key:
        cfg.api_key_cipher = encrypt(body.api_key, aad=f"llm:{cfg.id}")
        cfg.status = "unchecked"
    if body.fallback_config_id is not None:
        cfg.fallback_config_id = (uuid.UUID(body.fallback_config_id)
                                  if body.fallback_config_id else None)
    await _set_default_flags(db, cfg, body.is_default, body.is_default_embedding)
    db.add(AuditLog(user_id=user.id, username=user.username, action="llm.update",
                    resource_type="llm_config", resource_id=str(cfg.id)))
    await db.commit()
    await db.refresh(cfg)
    invalidate_config_cache()  # 配置变更（endpoint/模型/密钥/默认标记）立即生效
    return cfg


async def _set_default_flags(db, cfg, is_default, is_default_embedding) -> None:
    if is_default:
        for other in (await db.execute(select(LlmConfig).where(
                LlmConfig.is_default.is_(True), LlmConfig.kind == ModelKind.CHAT))).scalars():
            other.is_default = False
        cfg.is_default = True
    if is_default_embedding:
        for other in (await db.execute(select(LlmConfig).where(
                LlmConfig.is_default_embedding.is_(True), LlmConfig.kind == ModelKind.EMBEDDING))).scalars():
            other.is_default_embedding = False
        cfg.is_default_embedding = True


@router.delete("/configs/{config_id}")
async def delete_config(config_id: str, db: AsyncSession = Depends(get_db),
                        user=Depends(require_admin)):
    cfg = await db.get(LlmConfig, uuid.UUID(config_id))
    if cfg is None:
        raise NotFoundError("配置不存在")
    if cfg.is_default:
        raise ValidationError("默认配置不可删除，请先将其他配置设为默认")
    await db.delete(cfg)
    await db.commit()
    invalidate_config_cache()
    return {"ok": True}


@router.post("/configs/{config_id}/health")
async def health_check(config_id: str, db: AsyncSession = Depends(get_db),
                       _=Depends(require_admin)):
    result = await check_config(uuid.UUID(config_id))
    invalidate_config_cache()
    return result


@router.post("/configs/{config_id}/activate")
async def activate(config_id: str, body: dict, db: AsyncSession = Depends(get_db),
                   user=Depends(require_admin)):
    """设为默认（body: {"as_embedding": bool}）。切换策略支持 immediate/new_session。"""
    cfg = await db.get(LlmConfig, uuid.UUID(config_id))
    if cfg is None:
        raise NotFoundError("配置不存在")
    as_emb = bool((body or {}).get("as_embedding")) or cfg.kind == ModelKind.EMBEDDING
    await _set_default_flags(db, cfg, not as_emb, as_emb)
    db.add(AuditLog(user_id=user.id, username=user.username, action="llm.activate",
                    resource_type="llm_config", resource_id=str(cfg.id),
                    detail={"as_embedding": as_emb, "policy": cfg.switch_policy}))
    await db.commit()
    invalidate_config_cache()  # 立即生效，无需重启
    return {"ok": True, "switch_policy": cfg.switch_policy}


# ---------- 场景绑定 ----------

@router.get("/scene-bindings")
async def get_bindings(db: AsyncSession = Depends(get_db), _=Depends(require_admin)):
    rows = (await db.execute(select(LlmSceneBinding))).scalars().all()
    return {"scenes": SCENES,
            "bindings": [{"scene": r.scene.value, "config_id": str(r.config_id)} for r in rows]}


@router.put("/scene-bindings")
async def set_bindings(body: dict, db: AsyncSession = Depends(get_db),
                       _=Depends(require_admin)):
    """body: {"bindings": [{"scene": "...", "config_id": "..."}]}（全量覆盖，config_id 为空删除绑定）"""
    for r in (await db.execute(select(LlmSceneBinding))).scalars():
        await db.delete(r)
    for b in (body or {}).get("bindings", []):
        if b.get("config_id"):
            db.add(LlmSceneBinding(scene=LlmScene(b["scene"]),
                                   config_id=uuid.UUID(b["config_id"])))
    await db.commit()
    invalidate_config_cache()
    return {"ok": True}


# ---------- 观测 ----------

@router.get("/usage")
async def usage(group_by: str = "day", days: int = 14,
                db: AsyncSession = Depends(get_db), _=Depends(require_admin)):
    """用量统计：SQL 侧 group_by 聚合（不再全量拉日志进内存）。"""
    since = utcnow() - timedelta(days=min(days, 90))
    if group_by == "day":
        # func.date 在 SQLite/MySQL 均可用，结果统一截成 YYYY-MM-DD 字符串
        key_expr = func.date(LlmCallLog.created_at)
    elif group_by == "scene":
        key_expr = func.coalesce(func.nullif(LlmCallLog.scene, ""), "default")
    else:
        key_expr = func.coalesce(func.nullif(LlmCallLog.model, ""), "unknown")
    q = (select(key_expr.label("k"),
                func.coalesce(func.sum(LlmCallLog.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(LlmCallLog.output_tokens), 0).label("output_tokens"),
                func.count(LlmCallLog.id).label("calls"),
                func.coalesce(func.sum(
                    case((LlmCallLog.status != CallStatus.OK, 1), else_=0)), 0).label("errors"))
         .where(LlmCallLog.created_at >= since)
         .group_by("k").order_by("k"))
    rows = (await db.execute(q)).all()
    return {"group_by": group_by,
            "rows": [{"key": str(k)[:10] if group_by == "day" else str(k),
                      "input_tokens": int(it), "output_tokens": int(ot),
                      "calls": int(c), "errors": int(e)}
                     for k, it, ot, c, e in rows]}


@router.get("/call-logs")
async def call_logs(status: str | None = None, scene: str | None = None,
                    page: int = 1, page_size: int = 50,
                    db: AsyncSession = Depends(get_db), _=Depends(require_admin)):
    page, page_size = max(page, 1), min(max(page_size, 1), 200)
    q = select(LlmCallLog).order_by(LlmCallLog.created_at.desc())
    if status:
        q = q.where(LlmCallLog.status == status)
    if scene:
        q = q.where(LlmCallLog.scene == scene)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    rows = (await db.execute(q.offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"total": total, "page": page,
            "rows": [{"id": str(r.id), "config_id": str(r.config_id) if r.config_id else None,
                      "scene": r.scene, "protocol": r.protocol, "model": r.model,
                      "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                      "latency_ms": r.latency_ms, "status": r.status.value,
                      "error_brief": r.error_brief,
                      "created_at": r.created_at.isoformat()} for r in rows]}
