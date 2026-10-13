"""AI 数据治理 API（PRD 3.3F）：六域巡检 + LLM 治理报告 + 治理项转任务/修订 + 质量探查。

安全边界：
- 巡检只读元数据与任务登记，不触碰业务数据
- 质量探查为只读 SELECT 抽样，继承数据权限（无权限的表不可探查），采样上限 2000 行
- 治理项转任务仅创建草稿（不执行 DDL、不推送调度）
"""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import accessible_datasource_ids, get_current_user
from app.core.errors import NotFoundError, PermissionDenied, ValidationError
from app.db.models import (AuditLog, DataSource, EtlTask, EtlTaskSource, GovernanceRun,
                           MetaColumn, MetaTable, Role)
from app.db.models.llm import LlmScene
from app.db.session import get_db
from app.llm.gateway import gateway
from app.llm.types import ChatMessage
from app.services import governance as gov

router = APIRouter(prefix="/governance", tags=["governance"])


def _run_row(r: GovernanceRun, with_findings: bool) -> dict:
    out = {"id": str(r.id), "stats": r.stats or {}, "report": r.report,
           "created_at": r.created_at.isoformat(),
           "finding_count": len(r.findings or [])}
    if with_findings:
        out["findings"] = r.findings or []
    return out


@router.post("/run")
async def run_governance(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """发起一次治理巡检：规则引擎（必有）+ LLM 治理报告（可选增强），快照留档。"""
    ds_ids = await accessible_datasource_ids(db, user)
    findings = await gov.run_rule_engine(db, ds_ids)

    stats: dict[str, int] = {}
    for f in findings:
        stats[f["domain"]] = stats.get(f["domain"], 0) + 1

    # LLM 治理报告（失败不影响规则结论）
    report = ""
    if findings:
        try:
            prompt = gov.REPORT_PROMPT.replace(
                "{findings}", json.dumps(findings[:60], ensure_ascii=False, indent=1))
            resp = await gateway.chat(LlmScene.GOVERNANCE,
                                      [ChatMessage("user", prompt)], max_tokens=2000)
            report = resp.text.strip()
        except Exception:
            report = ""

    run = GovernanceRun(user_id=user.id, findings=findings, report=report, stats=stats)
    db.add(run)
    await db.flush()
    db.add(AuditLog(user_id=user.id, username=user.username, action="governance.run",
                    resource_type="governance_run", resource_id=str(run.id),
                    detail={"finding_count": len(findings), "stats": stats}))
    await db.commit()
    return _run_row(run, with_findings=True)


@router.get("/runs")
async def list_runs(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """巡检快照列表：普通用户仅见本人发起的，admin 全量。"""
    q = select(GovernanceRun).order_by(GovernanceRun.created_at.desc()).limit(20)
    if user.role != Role.ADMIN:
        q = q.where(GovernanceRun.user_id == user.id)
    rows = (await db.execute(q)).scalars()
    return {"runs": [_run_row(r, with_findings=False) for r in rows]}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, db: AsyncSession = Depends(get_db),
                  user=Depends(get_current_user)):
    run = await db.get(GovernanceRun, uuid.UUID(run_id))
    if run is None:
        raise NotFoundError("巡检记录不存在")
    if user.role != Role.ADMIN and str(run.user_id) != str(user.id):
        raise PermissionDenied("无权查看该巡检记录")
    return _run_row(run, with_findings=True)


class ToTaskIn(BaseModel):
    title: str
    object: str = ""
    suggestion: str = ""
    layer: str = ""
    datasource_id: str | None = None


@router.post("/findings/to-task")
async def finding_to_task(body: ToTaskIn, db: AsyncSession = Depends(get_db),
                          user=Depends(get_current_user)):
    """治理项一键转建模任务草稿（DDL 不执行、不推送，进入任务列表人工处理）。"""
    if not body.title.strip():
        raise ValidationError("标题不能为空")
    if body.datasource_id:
        ds_ids = await accessible_datasource_ids(db, user)
        if ds_ids is not None and uuid.UUID(body.datasource_id) not in ds_ids:
            raise PermissionDenied("没有该数据源的访问权限")
    t = EtlTask(user_id=user.id,
                datasource_id=uuid.UUID(body.datasource_id) if body.datasource_id else None,
                name=f"[治理] {body.title.strip()[:50]}",
                target_table=body.object if body.object else "",
                layer=body.layer, notes=f"来源：AI 数据治理\n建议：{body.suggestion}",
                source=EtlTaskSource.MANUAL)
    db.add(t)
    await db.flush()
    db.add(AuditLog(user_id=user.id, username=user.username, action="governance.to_task",
                    resource_type="etl_task", resource_id=str(t.id),
                    detail={"title": body.title, "object": body.object}))
    await db.commit()
    return {"task_id": str(t.id), "name": t.name}


class ProbeIn(BaseModel):
    datasource_id: str
    table: str
    columns: list[str] = []     # 空 = 取该表前 12 个字段


@router.post("/probe")
async def probe_quality(body: ProbeIn, db: AsyncSession = Depends(get_db),
                        user=Depends(get_current_user)):
    """数据质量探查：只读 SELECT 抽样统计（行数/空值率/distinct 比），继承数据权限。

    结果包含规则发现（空值异常/主键重复风险）+ 解决建议。
    """
    ds_ids = await accessible_datasource_ids(db, user)
    ds_uuid = uuid.UUID(body.datasource_id)
    if ds_ids is not None and ds_uuid not in ds_ids:
        raise PermissionDenied("没有该数据源的查询权限，无法探查")
    ds = await db.get(DataSource, ds_uuid)
    if ds is None:
        raise NotFoundError("数据源不存在")
    mt = (await db.execute(select(MetaTable).where(
        MetaTable.datasource_id == ds.id,
        MetaTable.table_name == body.table))).scalar_one_or_none()
    if mt is None:
        raise NotFoundError(f"表「{body.table}」不在元数据中，请先扫描")

    meta_cols = list((await db.execute(
        select(MetaColumn).where(MetaColumn.table_id == mt.id)
        .order_by(MetaColumn.column_name))).scalars())
    known = {c.column_name for c in meta_cols}
    cols = body.columns[:12]
    if cols:
        bad = [c for c in cols if c not in known]
        if bad:
            raise ValidationError(f"字段未在元数据中登记：{'、'.join(bad[:3])}（请先扫描该表）")
    else:
        cols = [c.column_name for c in meta_cols[:12]]
    if not cols:
        raise ValidationError("该表无可用字段")

    from app.connectors.registry import get_connector
    conn = get_connector(ds)
    stats = await conn.sample_column_stats(body.table, cols, sample_rows=2000)
    if not stats:
        raise ValidationError("采样失败（数据源不可达或表不存在），请检查数据源连通性")

    pk_cols = {c.column_name for c in meta_cols if c.is_primary_key}
    findings: list[dict] = []
    for c, s in stats.items():
        non_null = s.get("non_null", 0)
        distinct = s.get("distinct", 0)
        # 空值异常：采样 2000 行但非空占比 < 50%
        if non_null and non_null < 1000:
            findings.append({"level": "warn", "column": c,
                             "issue": f"字段 {c} 空值率高（采样 2000 行仅 {non_null} 行非空）",
                             "suggestion": "排查上游写入；加工时补默认值或过滤（COALESCE/WHERE IS NOT NULL）"})
        # 主键/疑似主键重复风险
        is_pk = c in pk_cols or c.lower() == "id" or c.lower().endswith("_id")
        if is_pk and non_null > 100 and s.get("ratio") is not None and s["ratio"] < 0.999:
            findings.append({"level": "warn", "column": c,
                             "issue": f"疑似主键字段 {c} 存在重复"
                                      f"（{non_null} 行中 {distinct} 个不同值）",
                             "suggestion": "在加工 SQL 中加去重（ROW_NUMBER/GROUP BY），"
                                           "并排查上游重复推送"})
        # 维度取值异常分散
        if non_null > 100 and distinct == non_null and not is_pk:
            findings.append({"level": "info", "column": c,
                             "issue": f"字段 {c} 在采样中全部不重复（{non_null} 行）",
                             "suggestion": "若该字段应为维度（如渠道/状态），取值异常分散，"
                                           "请确认上游解析逻辑"})
    db.add(AuditLog(user_id=user.id, username=user.username, action="governance.probe",
                    resource_type="meta_table", resource_id=str(mt.id),
                    detail={"table": body.table, "columns": cols, "sample_rows": 2000}))
    await db.commit()
    return {"table": body.table, "columns": cols, "sample_rows": 2000,
            "stats": stats, "findings": findings}
