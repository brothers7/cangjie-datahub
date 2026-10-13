"""ETL 建模任务 API（PRD 3.3D 自动化建模模块）：CRUD / 版本快照 / 校验 / 推送 / 回读。

任务来源：对话 ETL 助手转入、AI 辅助新建、手工建模、Azkaban 导入。
系统绝不执行 DDL/写操作；推送 Azkaban 前端二次确认并审计。
"""
from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.skills import etl as etl_skill
from app.api.deps import get_current_user
from app.core.errors import NotFoundError, PermissionDenied, ValidationError
from app.db.models import (AuditLog, AzkabanConfig, CustomSkill, EtlTask, EtlTaskSource, EtlTaskStatus,
                           EtlTaskVersion, MetaTable, Role, SkillStatus, SkillType)
from app.db.models.base import utcnow
from app.db.session import get_db, get_session_factory
from app.services import azkaban as azk

router = APIRouter(prefix="/etl/tasks", tags=["etl-tasks"])

SOURCES = {s.value for s in EtlTaskSource}


class EtlTaskIn(BaseModel):
    name: str = ""
    target_table: str = ""
    layer: str = ""
    dialect: str = ""
    schedule: str = ""
    cron: str = ""
    depends_on: list[str] = []
    ddl: str = ""
    etl_sql: str = ""
    source_tables: list = []
    field_mapping: list = []
    notes: str = ""
    warnings: list = []
    source: str = "manual"
    datasource_id: str | None = None
    conversation_id: str | None = None
    modeling_session_id: str | None = None   # 来源建模会话（AI 方案卡片单表落地）
    sched_enabled: bool = False              # 启用内置调度（PRD 3.3G）


class EtlTaskUpdate(BaseModel):
    name: str | None = None
    target_table: str | None = None
    schedule: str | None = None
    cron: str | None = None
    depends_on: list[str] | None = None
    ddl: str | None = None
    etl_sql: str | None = None
    notes: str | None = None
    sched_enabled: bool | None = None


def _row(t: EtlTask) -> dict:
    return {"id": str(t.id), "name": t.name, "target_table": t.target_table,
            "layer": t.layer, "dialect": t.dialect, "schedule": t.schedule,
            "cron": t.cron, "depends_on": t.depends_on or [],
            "ddl": t.ddl, "etl_sql": t.etl_sql,
            "source_tables": t.source_tables or [], "field_mapping": t.field_mapping or [],
            "notes": t.notes, "warnings": t.warnings or [],
            "status": t.status.value, "source": t.source.value,
            "azkaban_project": t.azkaban_project, "azkaban_flow": t.azkaban_flow,
            "pushed_at": t.pushed_at.isoformat() if t.pushed_at else None,
            "datasource_id": str(t.datasource_id) if t.datasource_id else None,
            "sched_enabled": bool(t.sched_enabled),
            "approved_by": t.approved_by or "",
            "approved_at": t.approved_at.isoformat() if t.approved_at else None,
            "created_at": t.created_at.isoformat(),
            "updated_at": t.updated_at.isoformat()}


async def _get_owned(task_id: str, db: AsyncSession, user) -> EtlTask:
    t = await db.get(EtlTask, uuid.UUID(task_id))
    if t is None:
        raise NotFoundError("任务不存在")
    if user.role != Role.ADMIN and str(t.user_id) != str(user.id):
        raise PermissionDenied("无权访问该任务")
    return t


async def _snapshot(db: AsyncSession, t: EtlTask, username: str) -> None:
    """保存当前内容为版本快照。"""
    max_ver = (await db.execute(
        select(func.max(EtlTaskVersion.version)).where(EtlTaskVersion.task_id == t.id)
    )).scalar() or 0
    db.add(EtlTaskVersion(task_id=t.id, version=max_ver + 1,
                          ddl=t.ddl, etl_sql=t.etl_sql, saved_by=username))


async def _check_dep_cycle(db: AsyncSession, t: EtlTask) -> None:
    """任务级依赖防环（PRD 3.3G）：沿 depends_on 深度遍历，回到自身即拒绝。"""
    deps = [str(d) for d in (t.depends_on or []) if d]
    if not deps:
        return
    all_tasks = (await db.execute(select(EtlTask.id, EtlTask.depends_on))).all()
    graph = {str(r[0]): [str(d) for d in (r[1] or [])] for r in all_tasks}
    seen, stack = set(), list(deps)
    while stack:
        cur = stack.pop()
        if cur == str(t.id):
            raise ValidationError("依赖存在循环（当前任务出现在自己的上游链路中），请调整依赖任务")
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(graph.get(cur, []))


@router.get("")
async def list_tasks(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    q = select(EtlTask).order_by(EtlTask.updated_at.desc())
    if user.role != Role.ADMIN:
        q = q.where(EtlTask.user_id == user.id)
    return [_row(t) for t in (await db.execute(q)).scalars()]


@router.post("")
async def create_task(body: EtlTaskIn, db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    if body.source not in SOURCES:
        raise ValidationError(f"source 必须是 {'/'.join(sorted(SOURCES))}")
    if body.datasource_id:
        from app.api.deps import accessible_datasource_ids
        allowed = await accessible_datasource_ids(db, user)
        if allowed is not None and uuid.UUID(body.datasource_id) not in allowed:
            raise PermissionDenied("没有该数据源的访问权限")
    t = EtlTask(user_id=user.id,
                conversation_id=uuid.UUID(body.conversation_id) if body.conversation_id else None,
                datasource_id=uuid.UUID(body.datasource_id) if body.datasource_id else None,
                modeling_session_id=(uuid.UUID(body.modeling_session_id)
                                     if body.modeling_session_id else None),
                name=body.name or body.target_table or "未命名任务",
                target_table=body.target_table, layer=body.layer, dialect=body.dialect,
                schedule=body.schedule, cron=body.cron, depends_on=body.depends_on,
                ddl=body.ddl, etl_sql=body.etl_sql,
                source_tables=body.source_tables, field_mapping=body.field_mapping,
                notes=body.notes, warnings=body.warnings, source=EtlTaskSource(body.source),
                sched_enabled=body.sched_enabled)
    db.add(t)
    await db.flush()
    await _check_dep_cycle(db, t)
    await _snapshot(db, t, user.username)
    db.add(AuditLog(user_id=user.id, username=user.username, action="etl_task.create",
                    resource_type="etl_task", resource_id=str(t.id),
                    detail={"target_table": t.target_table, "source": body.source}))
    await db.commit()
    return _row(t)


@router.patch("/{task_id}")
async def update_task(task_id: str, body: EtlTaskUpdate,
                      db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    t = await _get_owned(task_id, db, user)
    sql_changed = ((body.ddl is not None and body.ddl != t.ddl)
                   or (body.etl_sql is not None and body.etl_sql != t.etl_sql))
    if sql_changed:
        await _snapshot(db, t, user.username)   # 留档旧版本
    for f in ("name", "target_table", "schedule", "cron", "depends_on", "ddl", "etl_sql", "notes"):
        v = getattr(body, f)
        if v is not None:
            setattr(t, f, v)
    if body.sched_enabled is not None:
        t.sched_enabled = body.sched_enabled
    if body.depends_on is not None:
        await _check_dep_cycle(db, t)
    # 内容被人工改过 → 回到草稿态，要求重新推送；内置调度审核同步失效（PRD 3.3G 闸门一）
    if sql_changed:
        if t.status == EtlTaskStatus.PUSHED:
            t.status = EtlTaskStatus.DRAFT
        t.approved_by, t.approved_at = "", None
    await db.commit()
    return _row(t)


@router.delete("/{task_id}")
async def delete_task(task_id: str, db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    t = await _get_owned(task_id, db, user)
    await db.delete(t)
    db.add(AuditLog(user_id=user.id, username=user.username, action="etl_task.delete",
                    resource_type="etl_task", resource_id=task_id,
                    detail={"target_table": t.target_table}))
    await db.commit()
    return {"ok": True}


# ---------- 版本快照 ----------

@router.get("/{task_id}/versions")
async def list_versions(task_id: str, db: AsyncSession = Depends(get_db),
                        user=Depends(get_current_user)):
    t = await _get_owned(task_id, db, user)
    rows = (await db.execute(
        select(EtlTaskVersion).where(EtlTaskVersion.task_id == t.id)
        .order_by(EtlTaskVersion.version.desc()))).scalars().all()
    return [{"version": v.version, "saved_by": v.saved_by,
             "created_at": v.created_at.isoformat(),
             "ddl_len": len(v.ddl), "sql_len": len(v.etl_sql)} for v in rows]


@router.post("/{task_id}/rollback")
async def rollback(task_id: str, body: dict, db: AsyncSession = Depends(get_db),
                   user=Depends(get_current_user)):
    """回滚到指定版本（当前内容先留档）。"""
    t = await _get_owned(task_id, db, user)
    ver = (await db.execute(
        select(EtlTaskVersion).where(EtlTaskVersion.task_id == t.id,
                                     EtlTaskVersion.version == int((body or {}).get("version", 0)))
    )).scalar_one_or_none()
    if ver is None:
        raise NotFoundError("版本不存在")
    await _snapshot(db, t, user.username)
    t.ddl, t.etl_sql = ver.ddl, ver.etl_sql
    if t.status == EtlTaskStatus.PUSHED:
        t.status = EtlTaskStatus.DRAFT
    db.add(AuditLog(user_id=user.id, username=user.username, action="etl_task.rollback",
                    resource_type="etl_task", resource_id=str(t.id),
                    detail={"to_version": ver.version}))
    await db.commit()
    return _row(t)


# ---------- 校验 / 推送 / 回读 ----------

@router.post("/{task_id}/validate")
async def validate_task(task_id: str, db: AsyncSession = Depends(get_db),
                        user=Depends(get_current_user)):
    """对编辑器当前内容跑规范校验（语法 + 引用 + 命名/粒度/分区/安全）。"""
    t = await _get_owned(task_id, db, user)
    design = etl_skill.EtlDesign(target_table=t.target_table, layer=t.layer,
                                 schedule=t.schedule, ddl=t.ddl, etl_sql=t.etl_sql,
                                 dialect=t.dialect)
    if t.datasource_id:
        existing = set((await db.execute(
            select(MetaTable.table_name).where(MetaTable.datasource_id == t.datasource_id)
        )).scalars())
        table_cols = await etl_skill._table_columns(db, t.datasource_id, [])
    else:
        existing, table_cols = set(), {}
    errors = etl_skill.sqlglot_check(t.ddl, t.etl_sql, t.dialect or "mysql", table_cols)
    etl_skill.validate_design(design, existing, t.dialect or "mysql", t.name)
    t.warnings = errors + design.warnings
    # 数仓规范检查（PRD 3.3E）：启用规范的结构化开关逐项校验，违规定位到条款
    from app.services import dw_standards
    standards = await dw_standards.load_enabled(db)
    if standards:
        layer_rows = (await db.execute(
            select(MetaTable.table_name, MetaTable.layer)
            .where(*( [MetaTable.datasource_id == t.datasource_id] if t.datasource_id else []))
        )).all()
        layer_map = {(n or "").lower(): l.value for n, l in layer_rows}
        src_layers = {str(s.get("table", "")).lower(): layer_map.get(str(s.get("table", "")).lower(), "")
                      for s in (t.source_tables or []) if isinstance(s, dict)}
        t.warnings += dw_standards.check_rules(
            standards, table_name=t.target_table or t.name, layer=t.layer,
            ddl=t.ddl, etl_sql=t.etl_sql, source_layers=src_layers)
    await db.commit()
    return {"warnings": t.warnings, "ok": not errors}


@router.post("/{task_id}/push")
async def push_task(task_id: str, body: dict, db: AsyncSession = Depends(get_db),
                    user=Depends(get_current_user)):
    """推送任务当前内容到 Azkaban（前端已二次确认）。"""
    t = await _get_owned(task_id, db, user)
    if not t.etl_sql.strip():
        raise ValidationError("加工 SQL 为空，无可推送内容")
    cfg = (await db.execute(
        select(AzkabanConfig).order_by(AzkabanConfig.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if cfg is None or not cfg.url:
        return {"ok": False, "message": "未配置 Azkaban，请下载 zip 人工上传（或联系管理员配置）"}
    project = (body or {}).get("project", "").strip() or cfg.default_project
    cron = (body or {}).get("cron", "").strip() or t.cron
    # depends_on 语义为任务 id 列表 → 映射为上游 flow 名（按目标表推导，与生成规则一致）
    dep_flows: list[str] = []
    if t.depends_on:
        dep_tasks = (await db.execute(select(EtlTask).where(
            EtlTask.id.in_([str(d) for d in t.depends_on])))).scalars().all()
        dep_flows = [azk._flow_name(d.target_table) for d in dep_tasks if d.target_table]
    zip_bytes, flow = azk.build_flow_zip(target_table=t.target_table, etl_sql=t.etl_sql,
                                         ddl=t.ddl, dialect=t.dialect or "hive",
                                         notes=t.notes, depends_on=dep_flows or None)
    try:
        client = azk.client_from_config(cfg)
        await client.ensure_project(project, description="data-agent ETL 工程")
        await client.upload_zip(project, zip_bytes)
        msg = f"已上传到项目 {project}，flow: {flow}"
        if cron:
            await client.schedule_cron(project, flow, cron)
            t.cron = cron
            msg += f"，已设置 cron 调度 {cron}"
        t.status = EtlTaskStatus.PUSHED
        t.azkaban_project, t.azkaban_flow, t.pushed_at = project, flow, utcnow()
        ok = True
    except azk.AzkabanError as e:
        ok, msg = False, f"推送失败: {e}（可下载 zip 人工上传）"
    db.add(AuditLog(user_id=user.id, username=user.username, action="azkaban.push",
                    resource_type="etl_task", resource_id=str(t.id),
                    detail={"project": project, "flow": flow, "cron": cron,
                            "ok": ok, "message": msg[:300]}))
    await db.commit()
    return {"ok": ok, "message": msg, "project": project, "flow": flow,
            "url": f"{cfg.url.rstrip('/')}/index" if ok else ""}


@router.get("/{task_id}/executions")
async def task_executions(task_id: str, db: AsyncSession = Depends(get_db),
                          user=Depends(get_current_user)):
    """回读已推送 flow 的执行历史。"""
    t = await _get_owned(task_id, db, user)
    if not t.azkaban_project or not t.azkaban_flow:
        raise ValidationError("任务尚未推送到 Azkaban")
    cfg = (await db.execute(
        select(AzkabanConfig).order_by(AzkabanConfig.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if cfg is None or not cfg.url:
        raise ValidationError("未配置 Azkaban")
    try:
        rows = await azk.client_from_config(cfg).flow_executions(t.azkaban_project, t.azkaban_flow)
    except azk.AzkabanError as e:
        raise ValidationError(str(e)) from e
    return {"rows": [{"exec_id": r.get("execId"), "status": r.get("status"),
                      "submit_time": r.get("submitTime"), "end_time": r.get("endTime")}
                     for r in rows]}


# ---------- 调度平台导出（PRD 3.3D-4：scheduler Skill，可插拔调度平台） ----------

@router.get("/schedulers")
async def list_schedulers(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """可用的调度平台：内置 Azkaban（直连推送）+ 已启用的调度 Skill（导出工程包）。"""
    rows = (await db.execute(select(CustomSkill).where(
        CustomSkill.skill_type == SkillType.SCHEDULER,
        CustomSkill.status == SkillStatus.ENABLED))).scalars()
    return {"platforms": [
        {"key": "local", "name": "内置调度平台（自建引擎）", "kind": "local",
         "description": "cron + 任务级依赖，双重闸门后执行加工 SQL"},
        {"key": "azkaban", "name": "Azkaban（内置直连）", "kind": "builtin"},
        {"key": "airflow", "name": "Airflow（导出 DAG）", "kind": "builtin",
         "description": "生成 DAG py 供人工放入 dags 目录"},
    ] + [
        {"key": f"skill:{s.name}", "name": f"{s.name}（Skill v{s.version}）",
         "kind": "scheduler_skill", "description": s.description} for s in rows]}


@router.post("/{task_id}/export")
async def export_task_package(task_id: str, body: dict, db: AsyncSession = Depends(get_db),
                              user=Depends(get_current_user)):
    """经调度平台 Skill 把任务导出为目标平台工程文件（沙箱生成，仅下载不直连）。

    body: {"scheduler": "<skill 名称>"}；Azkaban 直连请用 /push 或 /azkaban/package。
    """
    t = await _get_owned(task_id, db, user)
    if not (t.ddl.strip() or t.etl_sql.strip()):
        raise ValidationError("任务内容为空，无可导出内容")
    name = str((body or {}).get("scheduler") or "").strip()
    if name == "airflow":   # 内置 Airflow 模板导出（PRD 3.3G，不直连写入）
        from app.services import airflow as af
        content = af.build_dag_py(name=t.name, target_table=t.target_table, layer=t.layer,
                                  cron=t.cron, etl_sql=t.etl_sql,
                                  depends_on_ids=[str(d) for d in (t.depends_on or [])])
        db.add(AuditLog(user_id=user.id, username=user.username, action="etl_task.export",
                        resource_type="etl_task", resource_id=str(t.id),
                        detail={"scheduler": "airflow", "ok": True}))
        await db.commit()
        return {"ok": True, "filename": f"{t.target_table or 'dag'}_dag.py", "content": content,
                "note": "将文件放入 Airflow dags 目录后启用；跨 DAG 依赖按需添加 ExternalTaskSensor",
                "log": ""}
    skill_row = (await db.execute(select(CustomSkill).where(
        CustomSkill.name == name, CustomSkill.skill_type == SkillType.SCHEDULER,
        CustomSkill.status == SkillStatus.ENABLED))).scalar_one_or_none()
    if skill_row is None:
        raise NotFoundError(f"调度平台 Skill「{name}」不存在或未启用")

    task_payload = {"name": t.name, "target_table": t.target_table, "layer": t.layer,
                    "dialect": t.dialect or "hive", "schedule": t.schedule, "cron": t.cron,
                    "depends_on": t.depends_on or [], "ddl": t.ddl, "etl_sql": t.etl_sql,
                    "notes": t.notes}
    from app.services import custom_skills as cs
    result = await cs.run_scheduler_skill(skill_row.files, skill_row.entry, task_payload)
    db.add(AuditLog(user_id=user.id, username=user.username, action="etl_task.export",
                    resource_type="etl_task", resource_id=str(t.id),
                    detail={"scheduler": name, "ok": result.get("ok", False)}))
    await db.commit()
    if not result.get("ok"):
        raise ValidationError(f"调度 Skill 执行失败：{result.get('error', '未知错误')}")
    pkg = result["result"]
    return {"ok": True, "filename": pkg["filename"], "content": pkg["content"],
            "note": pkg.get("note", ""), "log": result.get("log", "")}


# ---------- AI 对话式修订（PRD 3.3D-3，内置编码代理） ----------

class AiReviseIn(BaseModel):
    instruction: str
    history: list[dict] = []        # 前端维护的修订对话 [{role, content}]


@router.post("/{task_id}/ai-revise")
async def ai_revise(task_id: str, body: AiReviseIn,
                    db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """SSE：对当前任务提修改意见，代理产出修订（不自动应用，前端 diff 对照后人工接受）。"""
    from app.agent import coding_agent
    from app.agent.pipeline.context import PipelineContext
    from app.agent.pipeline.events import sse_format

    t = await _get_owned(task_id, db, user)
    if not body.instruction.strip():
        raise ValidationError("请描述修改意见")

    ctx = PipelineContext(user_id=str(user.id), username=user.username)
    task_snapshot = {"id": str(t.id), "target_table": t.target_table, "layer": t.layer,
                     "dialect": t.dialect or "hive", "ddl": t.ddl, "etl_sql": t.etl_sql,
                     "datasource_id": str(t.datasource_id) if t.datasource_id else None}
    instruction = body.instruction.strip()
    history = [{"role": h.get("role", "user"), "content": str(h.get("content", ""))[:2000]}
               for h in (body.history or [])[-8:]]
    user_id, username, is_admin = str(user.id), user.username, user.role == Role.ADMIN

    async def run():
        async with get_session_factory()() as sdb:
            try:
                ds_ids = None
                if not is_admin:
                    from app.db.models import DatasourcePermission
                    ds_ids = list((await sdb.execute(
                        select(DatasourcePermission.datasource_id).where(
                            DatasourcePermission.user_id == uuid.UUID(user_id),
                            DatasourcePermission.can_query.is_(True)))).scalars())
                scope = coding_agent.AgentScope(
                    db=sdb,
                    datasource_id=uuid.UUID(task_snapshot["datasource_id"])
                    if task_snapshot["datasource_id"] else None,
                    ds_ids=ds_ids, dialect=task_snapshot["dialect"])
                extra = (f"<任务>\n目标表：{task_snapshot['target_table']}（{task_snapshot['layer']} 层）\n"
                         f"方言：{task_snapshot['dialect']}\n"
                         f"<当前 DDL>\n{task_snapshot['ddl'][:4000]}\n</当前 DDL>\n"
                         f"<当前加工 SQL>\n{task_snapshot['etl_sql'][:8000]}\n</当前加工 SQL>\n</任务>")
                result = await coding_agent.run_agent(
                    coding_agent.MODE_REVISE, f"修改意见：{instruction}", scope,
                    history=history, extra_context=extra, ctx=ctx)
                sdb.add(AuditLog(user_id=uuid.UUID(user_id), username=username,
                                 action="etl_task.ai_revise", resource_type="etl_task",
                                 resource_id=task_snapshot["id"],
                                 detail={"instruction": instruction[:200],
                                         "ok": result.payload is not None,
                                         "engine": result.engine,
                                         "engine_version": result.engine_version,
                                         "steps": result.steps_used}))
                await sdb.commit()
                ctx.emit("revision", {"revision": result.payload,
                                      "warnings": result.warnings,
                                      "instruction": instruction,
                                      "engine": result.engine,
                                      "engine_version": result.engine_version})
                ctx.emit("done", {"error": result.error})
            except Exception as e:
                ctx.emit("error", {"code": "AGENT_ERROR",
                                   "message": f"AI 修订失败：{e.__class__.__name__}"})
                ctx.emit("done", {"error": str(e)})

    task = asyncio.create_task(run())

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

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
