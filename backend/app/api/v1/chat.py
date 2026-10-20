"""对话问答：会话 CRUD + SSE 消息流 + 取消。"""
from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.pipeline.events import sse_format
from app.agent.pipeline.orchestrator import start_pipeline
from app.api.deps import can_access_datasource, get_current_user
from app.core.errors import NotFoundError, PermissionDenied, ValidationError
from app.db.models import Conversation, DataSource, Message, User
from app.db.session import get_db
from app.schemas.entities import ConversationCreateIn, MessageIn

router = APIRouter(prefix="/chat", tags=["chat"])

# 会话级活动任务注册表（取消用）
_ACTIVE_TASKS: dict[str, asyncio.Task] = {}


async def _validate_llm_config(db: AsyncSession, config_id) -> uuid.UUID | None:
    """校验会话要绑定的 LLM 配置存在且为对话模型；None/空 = 跟随默认。"""
    if not config_id:
        return None
    from app.db.models.llm import LlmConfig, ModelKind
    try:
        cfg = await db.get(LlmConfig, uuid.UUID(str(config_id)))
    except (ValueError, AttributeError):
        cfg = None
    if cfg is None:
        raise ValidationError("所选模型配置不存在")
    if cfg.kind != ModelKind.CHAT:
        raise ValidationError("该配置是 embedding 模型，不能用于对话")
    return cfg.id


@router.get("/conversations")
async def list_conversations(user: User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(Conversation)
        .where(Conversation.user_id == user.id, Conversation.status == "active")
        .order_by(Conversation.last_active_at.desc().nullslast())
        .limit(100))).scalars().all()
    return [{"id": str(r.id), "title": r.title,
             "datasource_id": str(r.datasource_id) if r.datasource_id else None,
             "llm_config_id": str(r.llm_config_id) if r.llm_config_id else None,
             "message_count": r.message_count,
             "last_active_at": r.last_active_at.isoformat() if r.last_active_at else None,
             "created_at": r.created_at.isoformat()} for r in rows]


@router.post("/conversations")
async def create_conversation(body: ConversationCreateIn,
                              user: User = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    ds_id = None
    if body.datasource_id:
        ds = await db.get(DataSource, uuid.UUID(body.datasource_id))
        if ds is None:
            raise NotFoundError("数据源不存在")
        if not await can_access_datasource(db, user, ds.id):
            raise PermissionDenied("无该数据源的查询权限")
        ds_id = ds.id
    llm_cfg_id = await _validate_llm_config(db, body.llm_config_id)
    conv = Conversation(user_id=user.id, title=body.title or "新会话", datasource_id=ds_id,
                        llm_config_id=llm_cfg_id)
    db.add(conv)
    await db.commit()
    return {"id": str(conv.id), "title": conv.title}


@router.patch("/conversations/{cid}")
async def update_conversation(cid: str, body: dict, user: User = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    conv = await db.get(Conversation, uuid.UUID(cid))
    if conv is None or conv.user_id != user.id:
        raise NotFoundError("会话不存在")
    if "title" in body:
        conv.title = str(body["title"])[:120]
    if "datasource_id" in body and body["datasource_id"]:
        ds = await db.get(DataSource, uuid.UUID(body["datasource_id"]))
        if ds is None or not await can_access_datasource(db, user, ds.id):
            raise PermissionDenied("无该数据源的查询权限")
        conv.datasource_id = ds.id
    if "llm_config_id" in body:   # 允许切换/清除会话模型（null 清除）
        conv.llm_config_id = await _validate_llm_config(db, body.get("llm_config_id"))
    if body.get("archived"):
        conv.status = "archived"
    await db.commit()
    return {"ok": True}


@router.delete("/conversations/{cid}")
async def delete_conversation(cid: str, user: User = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    conv = await db.get(Conversation, uuid.UUID(cid))
    if conv is None or conv.user_id != user.id:
        raise NotFoundError("会话不存在")
    await db.delete(conv)
    await db.commit()
    return {"ok": True}


@router.get("/conversations/{cid}/messages")
async def list_messages(cid: str, user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    conv = await db.get(Conversation, uuid.UUID(cid))
    if conv is None or conv.user_id != user.id:
        raise NotFoundError("会话不存在")
    rows = (await db.execute(
        select(Message).where(Message.conversation_id == conv.id)
        .order_by(Message.created_at))).scalars().all()
    return [{"id": str(r.id), "role": r.role.value, "content": r.content,
             "payload": r.payload, "created_at": r.created_at.isoformat()} for r in rows]


@router.post("/conversations/{cid}/messages")
async def send_message(cid: str, body: MessageIn, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    """SSE 问答主入口。Accept: text/event-stream。"""
    conv = await db.get(Conversation, uuid.UUID(cid))
    if conv is None or conv.user_id != user.id:
        raise NotFoundError("会话不存在")
    # 数据源权限校验
    ds_id = conv.datasource_id or (conv.short_term_state or {}).get("datasource_id")
    if ds_id:
        ds = await db.get(DataSource, uuid.UUID(str(ds_id)))
        if ds is None or not await can_access_datasource(db, user, ds.id):
            raise PermissionDenied("无该数据源的查询权限")
    if not body.question.strip():
        raise ValidationError("问题不能为空")

    ctx, task = await start_pipeline(user_id=str(user.id), username=user.username,
                                     conversation_id=cid, question=body.question,
                                     confirm=body.confirm)
    old = _ACTIVE_TASKS.get(cid)
    if old is not None and not old.done():
        old.cancel()
    _ACTIVE_TASKS[cid] = task

    async def gen():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(ctx.events.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"   # 防代理超时
                    continue
                yield sse_format(ev["event"], ev["data"])
                if ev["event"] == "done":
                    break
            await task
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            _ACTIVE_TASKS.pop(cid, None)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


@router.post("/conversations/{cid}/cancel")
async def cancel(cid: str, user: User = Depends(get_current_user),
                 db: AsyncSession = Depends(get_db)):
    # 属主校验：不能取消他人的问答任务
    conv = await db.get(Conversation, uuid.UUID(cid))
    if conv is None:
        raise NotFoundError("会话不存在")
    if str(conv.user_id) != str(user.id):
        raise PermissionDenied("无权取消该会话的任务")
    task = _ACTIVE_TASKS.get(cid)
    if task is not None and not task.done():
        task.cancel()
        _ACTIVE_TASKS.pop(cid, None)
        return {"ok": True, "cancelled": True}
    return {"ok": True, "cancelled": False}
