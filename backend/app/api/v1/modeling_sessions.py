"""AI 建模建议会话 API（PRD 3.3D-1）：需求 → 方案 → 对话修订 → 接受一键落地。

- POST /modeling/sessions           创建会话（需求标题）
- POST /modeling/sessions/{id}/messages  SSE：发送需求/修订意见，代理循环步骤实时可见，
                                          结束产出 proposal 事件并落库
- POST /modeling/sessions/{id}/accept    接受某条方案：批量创建建模任务（草稿，绝不执行 DDL）
安全：代理工具白名单 + 步数上限（coding_agent）；落地仅建任务草稿，不执行不推送。
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import coding_agent
from app.agent.pipeline.context import PipelineContext
from app.agent.pipeline.events import sse_format
from app.api.deps import accessible_datasource_ids, get_current_user
from app.core.errors import NotFoundError, PermissionDenied, ValidationError
from app.db.models import (AuditLog, DatasourcePermission, DataSource, EtlTask, EtlTaskSource,
                           ModelingMessage, ModelingSession, ModelingSessionStatus, Role)
from app.db.models.base import utcnow
from app.db.session import get_db, get_session_factory

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/modeling/sessions", tags=["modeling-sessions"])


class SessionIn(BaseModel):
    title: str = ""
    requirement: str
    datasource_id: str | None = None
    llm_config_id: str | None = None      # 会话级模型选择（PRD 3.3D-1）


class SessionUpdate(BaseModel):
    llm_config_id: str | None = None      # 传 null 清除，回到场景绑定


async def _validate_llm(db: AsyncSession, config_id: str | None):
    if not config_id:
        return None
    from app.db.models import LlmConfig, ModelKind
    try:
        cfg = await db.get(LlmConfig, uuid.UUID(str(config_id)))
    except ValueError:
        cfg = None
    if cfg is None or cfg.kind != ModelKind.CHAT:
        raise ValidationError("对话模型配置不存在")
    return cfg.id


class MessageIn(BaseModel):
    content: str


class AcceptIn(BaseModel):
    message_id: str


def _session_row(s: ModelingSession, msg_count: int = 0) -> dict:
    return {"id": str(s.id), "title": s.title, "requirement": s.requirement,
            "status": s.status.value, "message_count": msg_count,
            "datasource_id": str(s.datasource_id) if s.datasource_id else None,
            "llm_config_id": str(s.llm_config_id) if s.llm_config_id else None,
            "created_at": s.created_at.isoformat(), "updated_at": s.updated_at.isoformat()}


def _msg_row(m: ModelingMessage) -> dict:
    return {"id": str(m.id), "seq": m.seq, "role": m.role, "content": m.content,
            "proposal": m.proposal, "steps": m.steps or [],
            "created_at": m.created_at.isoformat()}


async def _get_owned(session_id: str, db: AsyncSession, user) -> ModelingSession:
    s = await db.get(ModelingSession, uuid.UUID(session_id))
    if s is None:
        raise NotFoundError("建模会话不存在")
    if user.role != Role.ADMIN and str(s.user_id) != str(user.id):
        raise PermissionDenied("无权访问该会话")
    return s


@router.get("")
async def list_sessions(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    q = select(ModelingSession).order_by(ModelingSession.updated_at.desc())
    if user.role != Role.ADMIN:
        q = q.where(ModelingSession.user_id == user.id)
    rows = (await db.execute(q)).scalars().all()
    out = []
    for s in rows:
        n = (await db.execute(select(func.count()).select_from(ModelingMessage)
                              .where(ModelingMessage.session_id == s.id))).scalar() or 0
        out.append(_session_row(s, n))
    return out


@router.post("")
async def create_session(body: SessionIn, db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    if not body.requirement.strip():
        raise ValidationError("请描述数据需求")
    ds_id = uuid.UUID(body.datasource_id) if body.datasource_id else None
    if ds_id:
        ds_ids = await accessible_datasource_ids(db, user)
        if ds_ids is not None and ds_id not in ds_ids:
            raise PermissionDenied("无该数据源的访问权限")
    s = ModelingSession(user_id=user.id, datasource_id=ds_id,
                        title=body.title.strip() or body.requirement.strip()[:30],
                        requirement=body.requirement.strip(),
                        llm_config_id=await _validate_llm(db, body.llm_config_id))
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return _session_row(s)


@router.patch("/{session_id}")
async def update_session(session_id: str, body: SessionUpdate,
                         db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """切换/清除会话级模型（PRD 3.3D-1）。"""
    s = await _get_owned(session_id, db, user)
    s.llm_config_id = await _validate_llm(db, body.llm_config_id)
    s.updated_at = utcnow()
    await db.commit()
    return _session_row(s)


@router.get("/{session_id}")
async def get_session(session_id: str, db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    s = await _get_owned(session_id, db, user)
    msgs = (await db.execute(select(ModelingMessage)
                             .where(ModelingMessage.session_id == s.id)
                             .order_by(ModelingMessage.seq))).scalars().all()
    return {**_session_row(s, len(msgs)), "messages": [_msg_row(m) for m in msgs]}


@router.delete("/{session_id}")
async def delete_session(session_id: str, db: AsyncSession = Depends(get_db),
                         user=Depends(get_current_user)):
    s = await _get_owned(session_id, db, user)
    # 删除会话不删除已落地的任务（PRD 3.3D-1）
    await db.execute(ModelingMessage.__table__.delete().where(
        ModelingMessage.session_id == s.id))
    await db.delete(s)
    await db.commit()
    return {"ok": True}


_ACTIVE: dict[str, asyncio.Task] = {}


@router.post("/{session_id}/messages")
async def send_message(session_id: str, body: MessageIn,
                       db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """SSE：发送需求/修订意见，AI 建模工程师代理循环逐步可见。"""
    s = await _get_owned(session_id, db, user)
    if not body.content.strip():
        raise ValidationError("内容不能为空")
    if s.status == ModelingSessionStatus.ARCHIVED:
        raise ValidationError("会话已归档，请新建会话")

    # 历史（供代理理解上一版方案）
    msgs = (await db.execute(select(ModelingMessage)
                             .where(ModelingMessage.session_id == s.id)
                             .order_by(ModelingMessage.seq))).scalars().all()
    history = [{"role": m.role, "content": m.content, "proposal": m.proposal} for m in msgs]
    seq = (msgs[-1].seq + 1) if msgs else 0
    db.add(ModelingMessage(session_id=s.id, seq=seq, role="user",
                           content=body.content.strip()))
    s.updated_at = utcnow()
    await db.commit()

    datasource_id = str(s.datasource_id) if s.datasource_id else None
    llm_override = str(s.llm_config_id) if s.llm_config_id else None
    user_id, username, role = str(user.id), user.username, user.role.value

    ctx = PipelineContext(user_id=user_id, username=username)

    async def run():
        async with get_session_factory()() as sdb:
            try:
                ds_ids = None
                if role != Role.ADMIN:
                    ds_ids = list((await sdb.execute(
                        select(DatasourcePermission.datasource_id).where(
                            DatasourcePermission.user_id == uuid.UUID(user_id),
                            DatasourcePermission.can_query.is_(True)))).scalars())
                dialect = "hive"
                if datasource_id:
                    ds = await sdb.get(DataSource, uuid.UUID(datasource_id))
                    if ds is not None:
                        dialect = ds.db_type.value
                scope = coding_agent.AgentScope(
                    db=sdb,
                    datasource_id=uuid.UUID(datasource_id) if datasource_id else None,
                    ds_ids=ds_ids, dialect=dialect)
                result = await coding_agent.run_agent(
                    coding_agent.MODE_PROPOSE, body.content.strip(), scope,
                    history=history, ctx=ctx, override_config_id=llm_override)

                content = ""
                proposal = None
                if result.payload:
                    proposal = result.payload
                    content = proposal.get("summary") or "方案已生成"
                    if result.warnings:
                        content += "\n\n⚠ " + "；".join(result.warnings[:5])
                else:
                    content = f"未能产出方案：{result.error or '未知原因'}"
                step_dicts = [st.to_dict() for st in ctx.steps]
                sdb.add(ModelingMessage(session_id=uuid.UUID(session_id), seq=seq + 1,
                                        role="assistant", content=content,
                                        proposal=proposal, steps=step_dicts))
                sess = await sdb.get(ModelingSession, uuid.UUID(session_id))
                if sess is not None:
                    sess.updated_at = utcnow()
                sdb.add(AuditLog(user_id=uuid.UUID(user_id), username=username,
                                 action="modeling.propose", resource_type="modeling_session",
                                 resource_id=session_id,
                                 detail={"steps": result.steps_used,
                                         "engine": result.engine,
                                         "engine_version": result.engine_version,
                                         "ok": proposal is not None}))
                await sdb.commit()
                ctx.emit("proposal", {"message_seq": seq + 1, "content": content,
                                      "proposal": proposal, "warnings": result.warnings,
                                      "engine": result.engine,
                                      "engine_version": result.engine_version})
                ctx.emit("done", {"error": result.error})
            except Exception as e:
                logger.exception("建模会话代理失败")
                ctx.emit("error", {"code": "AGENT_ERROR",
                                   "message": f"建模代理失败：{e.__class__.__name__}"})
                ctx.emit("done", {"error": str(e)})

    task = asyncio.create_task(run())
    old = _ACTIVE.get(session_id)
    if old is not None and not old.done():
        old.cancel()
    _ACTIVE[session_id] = task

    async def gen():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(ctx.events.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                yield sse_format(ev["event"], ev["data"])
                if ev["event"] == "done":
                    break
            await task
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            _ACTIVE.pop(session_id, None)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/{session_id}/accept")
async def accept_proposal(session_id: str, body: AcceptIn,
                          db: AsyncSession = Depends(get_db),
                          user=Depends(get_current_user)):
    """接受建议：按方案批量创建建模任务（草稿）。绝不执行 DDL、不推送调度。"""
    s = await _get_owned(session_id, db, user)
    msg = await db.get(ModelingMessage, uuid.UUID(body.message_id))
    if msg is None or str(msg.session_id) != str(s.id) or not msg.proposal:
        raise NotFoundError("方案消息不存在")

    dialect = "hive"
    if s.datasource_id:
        ds = await db.get(DataSource, s.datasource_id)
        if ds is not None:
            dialect = ds.db_type.value

    results: list[dict] = []
    created: list[tuple[EtlTask, list[str]]] = []   # (任务, 来源表名列表)
    for tb in (msg.proposal.get("tables") or []):
        name = str(tb.get("name") or "").strip()
        if not name:
            results.append({"table": "?", "ok": False, "error": "缺少表名"})
            continue
        dup = (await db.execute(select(EtlTask).where(
            EtlTask.user_id == s.user_id, EtlTask.target_table == name))).scalars().first()
        if dup is not None:
            results.append({"table": name, "ok": False,
                            "error": f"已有任务指向 {name}（未重复创建）", "task_id": str(dup.id)})
            continue
        t = EtlTask(
            user_id=s.user_id, datasource_id=s.datasource_id,
            modeling_session_id=s.id,
            name=str(tb.get("comment") or name)[:255], target_table=name,
            layer=str(tb.get("layer") or "").lower(), dialect=dialect,
            schedule=str(tb.get("schedule") or ""), cron=str(tb.get("cron") or ""),
            depends_on=[],
            ddl=str(tb.get("ddl") or ""), etl_sql=str(tb.get("etl_sql") or ""),
            source_tables=[{"table": str(x)} for x in (tb.get("source_tables") or [])],
            notes=str(tb.get("rationale") or ""),
            warnings=[], source=EtlTaskSource.AI)
        db.add(t)
        await db.flush()
        created.append((t, [str(x) for x in (tb.get("source_tables") or [])]))
        from app.api.v1.etl_tasks import _snapshot
        await _snapshot(db, t, user.username)
        db.add(AuditLog(user_id=user.id, username=user.username,
                        action="modeling.accept", resource_type="etl_task",
                        resource_id=str(t.id),
                        detail={"session_id": session_id, "target_table": name}))
        results.append({"table": name, "ok": True, "task_id": str(t.id)})

    # 依赖自动推导：来源表 → 已有任务（含本批新建）目标表匹配 → depends_on 任务 id
    if created:
        existing = (await db.execute(select(EtlTask).where(
            EtlTask.user_id == s.user_id))).scalars().all()
        by_target: dict[str, str] = {}
        for t in existing:
            if t.target_table and t.target_table not in by_target:
                by_target[t.target_table] = str(t.id)
        for t, srcs in created:
            deps = sorted({by_target[src] for src in srcs
                           if src in by_target and by_target[src] != str(t.id)})
            t.depends_on = deps

    s.status = ModelingSessionStatus.ACCEPTED
    s.updated_at = utcnow()
    await db.commit()
    created = sum(1 for r in results if r["ok"])
    return {"results": results, "created": created,
            "message": f"已创建 {created} 个建模任务草稿（DDL 不会自动执行，需校验后人工推送）"}
