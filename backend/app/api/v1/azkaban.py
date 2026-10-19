"""Azkaban 调度平台对接 API（PRD 3.3B-6）。

- 配置管理（管理员）：连接信息加密存储，连通性测试
- 推送（登录用户 + 人工确认在前端完成）：生成 Flow 工程 zip → 创建项目 → 上传 → 可选 cron 调度
- 降级：未配置/失败时可下载 zip 人工上传
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.crypto import encrypt
from app.core.errors import ValidationError
from app.db.models import AuditLog, AzkabanConfig, IntegrationStatus
from app.db.models.base import utcnow
from app.db.session import get_db
from app.services import azkaban as azk

router = APIRouter(prefix="/azkaban", tags=["azkaban"])


class AzkabanConfigIn(BaseModel):
    url: str
    username: str
    password: str = ""            # 空 = 不修改已有密码
    default_project: str = "data-agent-etl"
    verify_tls: bool = False      # 校验 TLS 证书（默认关，兼容内网自签名）


class EtlPushIn(BaseModel):
    target_table: str = ""
    ddl: str = ""
    etl_sql: str = ""
    dialect: str = "hive"
    notes: str = ""
    project: str = ""             # 空 = 用默认项目
    cron: str = ""                # 空 = 只上传不调度


async def _get_config(db: AsyncSession) -> AzkabanConfig | None:
    return (await db.execute(
        select(AzkabanConfig).order_by(AzkabanConfig.created_at.desc()).limit(1)
    )).scalar_one_or_none()


def _out(cfg: AzkabanConfig | None) -> dict:
    if cfg is None:
        return {"configured": False, "url": "", "username": "", "verify_tls": False,
                "default_project": "", "status": "unchecked", "status_message": ""}
    return {"configured": bool(cfg.url), "url": cfg.url, "username": cfg.username,
            "default_project": cfg.default_project,
            "verify_tls": bool(getattr(cfg, "verify_tls", False)),
            "status": cfg.status.value, "status_message": cfg.status_message}


@router.get("/config")
async def get_config(db: AsyncSession = Depends(get_db), _=Depends(get_current_user)):
    """配置信息（不含密码）。登录可读——推送按钮需要知道是否已配置。"""
    return _out(await _get_config(db))


@router.put("/config")
async def put_config(body: AzkabanConfigIn, db: AsyncSession = Depends(get_db),
                     user=Depends(require_admin)):
    if not body.url.startswith(("http://", "https://")):
        raise ValidationError("Azkaban 地址必须是 http(s) URL")
    cfg = await _get_config(db)
    if cfg is None:
        cfg = AzkabanConfig(url=body.url, username=body.username,
                            default_project=body.default_project)
        db.add(cfg)
        await db.flush()
    else:
        cfg.url, cfg.username = body.url, body.username
        cfg.default_project = body.default_project
    if body.password:
        cfg.password_cipher = encrypt(body.password, aad=f"azkaban:{cfg.id}")
    cfg.verify_tls = body.verify_tls
    cfg.status = IntegrationStatus.UNCHECKED
    db.add(AuditLog(user_id=user.id, username=user.username, action="azkaban.config",
                    resource_type="azkaban", resource_id=str(cfg.id),
                    detail={"url": body.url, "username": body.username}))
    await db.commit()
    return _out(cfg)


@router.post("/test")
async def test_connection(db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    cfg = await _get_config(db)
    if cfg is None or not cfg.url:
        raise ValidationError("请先保存 Azkaban 配置")
    try:
        msg = await azk.client_from_config(cfg).test_connection()
        cfg.status, cfg.status_message = IntegrationStatus.HEALTHY, msg
        out = {"ok": True, "message": msg}
    except azk.AzkabanError as e:
        cfg.status, cfg.status_message = IntegrationStatus.ERROR, str(e)[:300]
        out = {"ok": False, "message": str(e)}
    await db.commit()
    return out


@router.post("/push")
async def push(body: EtlPushIn, db: AsyncSession = Depends(get_db),
               user=Depends(get_current_user)):
    """推送 ETL 方案到 Azkaban（前端已做二次确认）。DDL 只作为附件，不进调度节点。"""
    if not body.etl_sql.strip():
        raise ValidationError("加工 SQL 为空，无可推送内容")
    cfg = await _get_config(db)
    if cfg is None or not cfg.url:
        return {"ok": False, "message": "未配置 Azkaban，请下载 zip 人工上传（或联系管理员配置）"}
    project = body.project.strip() or cfg.default_project
    zip_bytes, flow = azk.build_flow_zip(target_table=body.target_table,
                                         etl_sql=body.etl_sql, ddl=body.ddl,
                                         dialect=body.dialect, notes=body.notes)
    try:
        client = azk.client_from_config(cfg)
        await client.ensure_project(project, description="data-agent ETL 工程")
        await client.upload_zip(project, zip_bytes)
        msg = f"已上传到项目 {project}，flow: {flow}"
        if body.cron.strip():
            await client.schedule_cron(project, flow, body.cron.strip())
            msg += f"，已设置 cron 调度 {body.cron.strip()}"
        ok = True
    except azk.AzkabanError as e:
        ok, msg = False, f"推送失败: {e}（可下载 zip 人工上传）"
    db.add(AuditLog(user_id=user.id, username=user.username, action="azkaban.push",
                    resource_type="azkaban", resource_id=project,
                    detail={"flow": flow, "target_table": body.target_table,
                            "cron": body.cron, "ok": ok, "message": msg[:300]}))
    await db.commit()
    return {"ok": ok, "message": msg, "project": project, "flow": flow,
            "url": f"{cfg.url.rstrip('/')}/index" if ok else ""}


@router.post("/package")
async def download_package(body: EtlPushIn, _=Depends(get_current_user)):
    """降级路径：直接下载 Flow 工程 zip，人工到 Azkaban 上传。"""
    zip_bytes, flow = azk.build_flow_zip(target_table=body.target_table,
                                         etl_sql=body.etl_sql, ddl=body.ddl,
                                         dialect=body.dialect, notes=body.notes)
    return Response(content=zip_bytes, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{flow}.zip"'})


@router.get("/executions")
async def executions(project: str, flow: str, db: AsyncSession = Depends(get_db),
                     _=Depends(get_current_user)):
    """回读 flow 最近执行历史。"""
    cfg = await _get_config(db)
    if cfg is None or not cfg.url:
        raise ValidationError("未配置 Azkaban")
    try:
        rows = await azk.client_from_config(cfg).flow_executions(project, flow)
    except azk.AzkabanError as e:
        raise ValidationError(str(e)) from e
    return {"rows": [{"exec_id": r.get("execId"), "status": r.get("status"),
                      "submit_time": r.get("submitTime"), "end_time": r.get("endTime")}
                     for r in rows]}


# ---------- Azkaban 元数据掌管（PRD 3.3D-3） ----------

@router.get("/metadata")
async def metadata(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """同步 Azkaban 项目/flow/调度/最近执行，并关联本地任务。"""
    from app.db.models import EtlTask

    cfg = await _get_config(db)
    if cfg is None or not cfg.url:
        return {"configured": False, "projects": []}
    try:
        client = azk.client_from_config(cfg)
        projects = await client.list_projects()
    except azk.AzkabanError as e:
        return {"configured": True, "error": str(e), "projects": []}

    # 本地任务关联索引：{(project, flow): task_id}
    local = {}
    for t in (await db.execute(select(EtlTask).where(EtlTask.azkaban_project != ""))).scalars():
        local[(t.azkaban_project, t.azkaban_flow)] = str(t.id)

    # projects×flows×2 的串行 HTTP 改为有界并发（Semaphore 限流），
    # 单个项目/flow 失败仍不拖垮整体（沿用原有容错语义）。
    sem = asyncio.Semaphore(8)

    async def _bounded(coro):
        async with sem:
            return await coro

    async def _flows_of(p: dict) -> list[str]:
        try:
            return await _bounded(client.list_flows(p["name"]))
        except azk.AzkabanError:
            return []

    async def _flow_info(p: dict, fl: str) -> dict:
        info: dict = {"flow": fl, "cron": "", "linked_task_id": local.get((p["name"], fl))}
        try:
            if p.get("project_id"):
                sched = await _bounded(client.fetch_schedule(p["project_id"], fl))
                if sched:
                    info["cron"] = sched["cron"]
            execs = await _bounded(client.flow_executions(p["name"], fl, limit=1))
            if execs:
                e = execs[0]
                info["last_status"] = e.get("status")
                info["last_end_time"] = e.get("endTime")
        except azk.AzkabanError:
            pass
        return info

    sel = projects[:20]
    flows_per_project = await asyncio.gather(*(_flows_of(p) for p in sel))
    out = []
    for p, flows in zip(sel, flows_per_project):
        infos = await asyncio.gather(*(_flow_info(p, fl) for fl in flows[:30]))
        out.append({"name": p["name"], "flows": list(infos)})
    return {"configured": True, "projects": out}


class AzkabanImportIn(BaseModel):
    project: str
    flow: str
    cron: str = ""


@router.post("/import")
async def import_flow(body: AzkabanImportIn, db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    """把 Azkaban 外部 flow 导入为本地建模任务（SQL 需人工补充/AI 生成）。"""
    from app.db.models import EtlTask, EtlTaskSource

    dup = (await db.execute(select(EtlTask).where(
        EtlTask.azkaban_project == body.project, EtlTask.azkaban_flow == body.flow))
    ).scalar_one_or_none()
    if dup:
        raise ValidationError("该 flow 已导入过（任务已存在）")
    t = EtlTask(user_id=user.id, name=body.flow, target_table=body.flow,
                cron=body.cron, source=EtlTaskSource.IMPORTED,
                azkaban_project=body.project, azkaban_flow=body.flow,
                notes=f"从 Azkaban 项目 {body.project} 导入；原始 SQL 未同步，请人工补充或用 AI 生成")
    db.add(t)
    await db.flush()
    db.add(AuditLog(user_id=user.id, username=user.username, action="azkaban.import",
                    resource_type="etl_task", resource_id=str(t.id),
                    detail={"project": body.project, "flow": body.flow}))
    await db.commit()
    return {"id": str(t.id), "ok": True}
