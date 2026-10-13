"""Airflow 调度平台对接 API（PRD 3.3G 调度平台配置）。

- 配置管理（管理员）：REST API 地址 + Basic 账号密码，密码 AES-256-GCM 加密存储
- 只读同步：DAG 列表 + 最近运行状态（供「调度平台配置」页展示）
- 写入不直连：任务经内置模板导出 DAG py（etl_tasks 导出端点），人工放入 dags 目录
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.crypto import encrypt
from app.core.errors import ValidationError
from app.db.models import AirflowConfig, AuditLog, IntegrationStatus
from app.db.session import get_db
from app.services import airflow as af

router = APIRouter(prefix="/airflow", tags=["airflow"])


class AirflowConfigIn(BaseModel):
    url: str
    username: str
    password: str = ""            # 空 = 不修改已有密码
    verify_tls: bool = False      # 校验 TLS 证书（默认关，兼容内网自签名）


async def _get_config(db: AsyncSession) -> AirflowConfig | None:
    return (await db.execute(
        select(AirflowConfig).order_by(AirflowConfig.created_at.desc()).limit(1)
    )).scalar_one_or_none()


def _out(cfg: AirflowConfig | None) -> dict:
    if cfg is None:
        return {"configured": False, "url": "", "username": "", "verify_tls": False,
                "status": "unchecked", "status_message": ""}
    return {"configured": bool(cfg.url), "url": cfg.url, "username": cfg.username,
            "verify_tls": bool(getattr(cfg, "verify_tls", False)),
            "status": cfg.status.value, "status_message": cfg.status_message}


@router.get("/config")
async def get_config(db: AsyncSession = Depends(get_db), _=Depends(get_current_user)):
    return _out(await _get_config(db))


@router.put("/config")
async def put_config(body: AirflowConfigIn, db: AsyncSession = Depends(get_db),
                     user=Depends(require_admin)):
    if not body.url.startswith(("http://", "https://")):
        raise ValidationError("Airflow 地址必须是 http(s) URL（REST API 所在地址）")
    cfg = await _get_config(db)
    if cfg is None:
        cfg = AirflowConfig(url=body.url, username=body.username)
        db.add(cfg)
        await db.flush()
    else:
        cfg.url, cfg.username = body.url, body.username
    if body.password:
        cfg.password_cipher = encrypt(body.password, aad=f"airflow:{cfg.id}")
    cfg.verify_tls = body.verify_tls
    cfg.status = IntegrationStatus.UNCHECKED
    db.add(AuditLog(user_id=user.id, username=user.username, action="airflow.config",
                    resource_type="airflow", resource_id=str(cfg.id),
                    detail={"url": body.url, "username": body.username}))
    await db.commit()
    return _out(cfg)


@router.post("/test")
async def test_connection(db: AsyncSession = Depends(get_db), user=Depends(require_admin)):
    cfg = await _get_config(db)
    if cfg is None or not cfg.url:
        raise ValidationError("请先保存 Airflow 配置")
    try:
        msg = await af.client_from_config(cfg).test_connection()
        cfg.status, cfg.status_message = IntegrationStatus.HEALTHY, msg
        out = {"ok": True, "message": msg}
    except af.AirflowError as e:
        cfg.status, cfg.status_message = IntegrationStatus.ERROR, str(e)[:300]
        out = {"ok": False, "message": str(e)}
    await db.commit()
    return out


@router.get("/metadata")
async def metadata(db: AsyncSession = Depends(get_db), _=Depends(get_current_user)):
    """远端 DAG 列表 + 最近运行状态（只读）。"""
    cfg = await _get_config(db)
    if cfg is None or not cfg.url:
        return {"configured": False, "dags": []}
    cli = af.client_from_config(cfg)
    try:
        dags = await cli.list_dags()
    except af.AirflowError as e:
        return {"configured": True, "error": str(e), "dags": []}
    out = []
    for d in dags[:60]:
        dag_id = d.get("dag_id", "")
        last = None
        try:
            run = await cli.latest_dag_run(dag_id)
            if run:
                last = {"state": run.get("state"), "start_date": run.get("start_date")}
        except af.AirflowError:
            pass
        out.append({"dag_id": dag_id, "is_paused": d.get("is_paused"),
                    "schedule": (d.get("schedule_interval") or {}).get("value")
                    if isinstance(d.get("schedule_interval"), dict) else d.get("schedule_interval"),
                    "last_run": last})
    return {"configured": True, "dags": out, "total": len(dags)}
