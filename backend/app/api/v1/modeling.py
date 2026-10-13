"""自动化建模模块 API（PRD 3.3D）：数仓模型全景 / AI 建模建议 / SQL 编辑器 AI 与代码提示。

SQL 编辑器的代码提示数据与 AI 生成都只基于元数据与 KG；AI 输出经安全剔除后才返回。
"""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.skills import etl as etl_skill
from app.agent.skills.sql_gen import sanitize_question
from app.agent.skills.table_qa import match_tables
from app.api.deps import accessible_datasource_ids, get_current_user
from app.core.errors import PermissionDenied, ValidationError
from app.core.utils import like_contains
from app.db.models import (EdgeType, EtlTask, EtlTaskStatus, KgEdge, MetaColumn, MetaTable,
                           Role, TableLayer)
from app.db.models.llm import LlmScene
from app.db.session import get_db
from app.llm.gateway import gateway
from app.llm.types import ChatMessage

router = APIRouter(prefix="/modeling", tags=["modeling"])

SQL_KEYWORDS = [
    "SELECT", "FROM", "WHERE", "GROUP BY", "ORDER BY", "INSERT OVERWRITE TABLE",
    "INSERT INTO", "PARTITION", "JOIN", "LEFT JOIN", "ON", "AS", "CASE WHEN", "WITH",
    "UNION ALL", "DISTINCT", "SUM", "COUNT", "COUNT(DISTINCT )", "AVG", "MAX", "MIN",
    "CREATE TABLE", "IF NOT EXISTS", "COMMENT", "LIMIT", "HAVING", "BETWEEN", "IN",
    "DATE_SUB", "DATE_FORMAT", "COALESCE", "CAST", "${ds}",
]


async def _scope_ds_ids(db: AsyncSession, user) -> list | None:
    return await accessible_datasource_ids(db, user)


# ---------- 数仓模型全景 ----------

@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """分层分布 + 健康度指标 + 任务/调度概览。"""
    ds_ids = await _scope_ds_ids(db, user)
    tq = select(MetaTable)
    if ds_ids is not None:
        tq = tq.where(MetaTable.datasource_id.in_(ds_ids))
    tables = (await db.execute(tq)).scalars().all()

    by_layer: dict[str, int] = {}
    uncommented = 0
    for t in tables:
        by_layer[t.layer.value] = by_layer.get(t.layer.value, 0) + 1
        if not t.comment:
            uncommented += 1

    table_ids = [t.id for t in tables]
    # 孤岛表：没有任何 TABLE_JOIN 边
    connected: set = set()
    if table_ids:
        for e in (await db.execute(
                select(KgEdge).where(KgEdge.edge_type == EdgeType.TABLE_JOIN,
                                     KgEdge.src_id.in_(table_ids)))).scalars():
            connected.add(e.src_id)
            connected.add(e.dst_id)
    isolated = [t.table_name for t in tables if t.id not in connected
                and t.layer not in (TableLayer.ODS, TableLayer.DIM)]

    # 有表无任务：ADS/DWS 表没有任何本地任务声明它为源或目标
    tids_with_task: set[str] = set()
    for t in (await db.execute(select(EtlTask))).scalars():
        if t.target_table:
            tids_with_task.add(t.target_table)
        for s in (t.source_tables or []):
            if isinstance(s, dict) and s.get("table"):
                tids_with_task.add(s["table"])
    no_task = [t.table_name for t in tables
               if t.layer in (TableLayer.DWS, TableLayer.ADS) and t.table_name not in tids_with_task]

    task_count = (await db.execute(select(func.count()).select_from(EtlTask))).scalar() or 0
    pushed = (await db.execute(select(func.count()).select_from(EtlTask)
                               .where(EtlTask.status == EtlTaskStatus.PUSHED))).scalar() or 0

    return {
        "table_total": len(tables),
        "by_layer": by_layer,
        "uncommented": uncommented,
        "uncommented_ratio": round(uncommented / len(tables), 3) if tables else 0,
        "isolated_tables": isolated[:30],
        "no_task_tables": no_task[:30],
        "task_total": task_count,
        "task_pushed": pushed,
    }


# ---------- 任务元数据（统一搜索，PRD 3.3D-4） ----------

@router.get("/task-metadata")
async def task_metadata(q: str = "", db: AsyncSession = Depends(get_db),
                        user=Depends(get_current_user)):
    """任务元数据页：本地建模任务 + 表元数据，统一模糊搜索（名称/注释/目标表）。

    权限：任务仅本人（管理员全量）；表元数据按数据源权限过滤。
    """
    kw = q.strip()
    like = like_contains(kw)

    tq = select(EtlTask).order_by(EtlTask.updated_at.desc())
    if user.role != Role.ADMIN:
        tq = tq.where(EtlTask.user_id == user.id)
    if kw:
        tq = tq.where((EtlTask.name.like(like, escape="\\"))
                      | (EtlTask.target_table.like(like, escape="\\"))
                      | (EtlTask.notes.like(like, escape="\\")))
    tasks = (await db.execute(tq.limit(50))).scalars()

    ds_ids = await _scope_ds_ids(db, user)
    mq = select(MetaTable).order_by(MetaTable.layer, MetaTable.table_name)
    if ds_ids is not None:
        mq = mq.where(MetaTable.datasource_id.in_(ds_ids))
    if kw:
        mq = mq.where((MetaTable.table_name.like(like, escape="\\"))
                      | (MetaTable.comment.like(like, escape="\\")))
    tables = list((await db.execute(mq.limit(60))).scalars())

    col_counts: dict = {}
    if tables:
        rows = (await db.execute(
            select(MetaColumn.table_id, func.count())
            .where(MetaColumn.table_id.in_([t.id for t in tables]))
            .group_by(MetaColumn.table_id))).all()
        col_counts = {str(r[0]): r[1] for r in rows}

    return {
        "tasks": [{"id": str(x.id), "name": x.name, "target_table": x.target_table,
                   "layer": x.layer, "cron": x.cron, "status": x.status.value,
                   "sched_enabled": bool(x.sched_enabled),
                   "source": x.source.value, "azkaban_project": x.azkaban_project,
                   "azkaban_flow": x.azkaban_flow,
                   "updated_at": x.updated_at.isoformat()} for x in tasks],
        "tables": [{"name": x.table_name, "layer": x.layer.value,
                    "comment": x.comment or "", "partition_cols": x.partition_cols or [],
                    "row_count": x.row_count, "col_count": col_counts.get(str(x.id), 0)}
                   for x in tables],
    }


# ---------- AI 建模建议 ----------

@router.post("/advice")
async def advice(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """建模规划建议：规则健康度结论（必有）+ LLM 规划报告（可选增强）。"""
    ov = await overview(db, user)
    findings: list[dict] = []

    if ov["uncommented_ratio"] > 0.3:
        findings.append({"level": "warn", "title": "注释覆盖率低",
                         "detail": f"{ov['uncommented']}/{ov['table_total']} 张表无注释，"
                                   "库表答疑与 AI 建模的准确性会受影响，建议先补齐核心表注释"})
    if ov["isolated_tables"]:
        findings.append({"level": "warn", "title": "存在孤岛表",
                         "detail": f"{len(ov['isolated_tables'])} 张表没有任何表关系登记："
                                   f"{ '、'.join(ov['isolated_tables'][:8]) }…，跨表分析无法路由"})
    if ov["by_layer"].get("dwd") and not ov["by_layer"].get("dws"):
        findings.append({"level": "block", "title": "缺失 DWS 汇总层",
                         "detail": "存在明细层但没有汇总层，高频聚合查询会反复扫明细，"
                                   "建议按核心业务域（交易/用户/商品）建 DWS 日汇总表"})
    if ov["no_task_tables"]:
        findings.append({"level": "info", "title": "汇总表无调度任务",
                         "detail": f"{len(ov['no_task_tables'])} 张 DWS/ADS 表没有对应加工任务："
                                   f"{ '、'.join(ov['no_task_tables'][:8]) }…，数据可能不是最新"})
    if not findings:
        findings.append({"level": "ok", "title": "模型健康",
                         "detail": "分层完整、注释覆盖率与调度覆盖良好，无显著结构性问题"})

    # LLM 规划报告（失败不影响规则结论）
    report = ""
    try:
        ds_ids = await _scope_ds_ids(db, user)
        tq = select(MetaTable).order_by(MetaTable.layer, MetaTable.table_name).limit(60)
        if ds_ids is not None:
            tq = tq.where(MetaTable.datasource_id.in_(ds_ids))
        tables = (await db.execute(tq)).scalars().all()
        catalog = "\n".join(f"- {t.table_name}（{t.layer.value}）：{t.comment or '无注释'}"
                            for t in tables)
        prompt = (f"你是资深数仓架构师。基于以下数仓全景给出建模规划建议（分层合理性、"
                  f"应新建的汇总表、重复加工合并、调度链路优化），分条输出，简明可落地。\n\n"
                  f"<全景统计>{ov}</全景统计>\n\n<表目录>\n{catalog}\n</表目录>")
        resp = await gateway.chat(LlmScene.ETL_DESIGN,
                                  [ChatMessage("user", prompt)], max_tokens=1500)
        report = resp.text.strip()
    except Exception:
        report = ""
    return {"findings": findings, "report": report, "overview": ov}


# ---------- SQL 编辑器支持 ----------

@router.get("/engine")
async def engine_status(user=Depends(get_current_user)):
    """AI 建模工程师引擎状态：内置编码代理 + 当前 modeling_agent 场景绑定的模型。"""
    from app.db.models.llm import LlmScene
    from app.llm.gateway import gateway
    info: dict = {"engine": "internal", "engine_name": "内置编码代理（Codex 核心架构）",
                  "scene": LlmScene.MODELING_AGENT.value,
                  "model": "", "config_id": "", "available": False}
    try:
        resolved = await gateway.resolve_scene(LlmScene.MODELING_AGENT)
        info.update(model=resolved.model, config_id=str(resolved.config_id),
                    available=True)
    except Exception:
        pass
    return info

@router.get("/completion-data")
async def completion_data(datasource_id: str | None = None,
                          db: AsyncSession = Depends(get_db),
                          user=Depends(get_current_user)):
    """编辑器代码提示数据：SQL 关键字 + 表（注释/分层）+ 字段（类型/注释/分区）。"""
    ds_ids = await _scope_ds_ids(db, user)
    tq = select(MetaTable).order_by(MetaTable.layer, MetaTable.table_name)
    if datasource_id:
        if ds_ids is not None and uuid.UUID(datasource_id) not in ds_ids:
            raise PermissionDenied("没有该数据源的访问权限")
        tq = tq.where(MetaTable.datasource_id == uuid.UUID(datasource_id))
    elif ds_ids is not None:
        tq = tq.where(MetaTable.datasource_id.in_(ds_ids))
    tables = (await db.execute(tq)).scalars().all()
    out_tables = []
    for t in tables[:200]:
        cols = (await db.execute(
            select(MetaColumn).where(MetaColumn.table_id == t.id))).scalars().all()
        out_tables.append({
            "name": t.table_name, "comment": t.comment, "layer": t.layer.value,
            "partition_cols": t.partition_cols or [],
            "columns": [{"name": c.column_name, "type": c.data_type,
                         "comment": c.comment} for c in cols[:80]],
        })
    return {"keywords": SQL_KEYWORDS, "tables": out_tables}


class AiEditIn(BaseModel):
    mode: str                    # generate / rewrite / explain / fix
    instruction: str = ""
    ddl: str = ""
    etl_sql: str = ""
    datasource_id: str | None = None
    dialect: str = "hive"


AI_EDIT_SYSTEM = """你是数仓 SQL 专家。只允许使用「数据目录」中存在的表和字段，禁止编造。
输出规则：
- mode=generate/rewrite/fix：只输出 SQL 本体（markdown 代码块可带可不带），不要解释。
- mode=explain：用简体中文解释 SQL 的逻辑、粒度、分区与注意点。
- fix 模式：根据给出的错误修复 SQL，保持原意。"""


@router.post("/ai-edit")
async def ai_edit(body: AiEditIn, db: AsyncSession = Depends(get_db),
                  user=Depends(get_current_user)):
    """SQL 编辑器 AI 能力：生成 / 改写 / 解释 / 修复。"""
    if body.mode not in ("generate", "rewrite", "explain", "fix"):
        raise ValidationError("mode 必须是 generate/rewrite/explain/fix")
    if body.mode == "generate" and not body.instruction.strip():
        raise ValidationError("请描述要生成的 SQL 需求")

    # 元数据上下文（命中表优先）
    catalog = ""
    if body.datasource_id:
        ds = uuid.UUID(body.datasource_id)
        ds_ids = await _scope_ds_ids(db, user)
        if ds_ids is not None and ds not in ds_ids:
            raise PermissionDenied("没有该数据源的访问权限")
        _, all_tables = await match_tables(db, ds, body.instruction or body.etl_sql[:200])
        lines = []
        for t in all_tables[:40]:
            cols = (await db.execute(
                select(MetaColumn).where(MetaColumn.table_id == t.id))).scalars().all()
            col_str = "、".join(f"{c.column_name}({c.comment})" if c.comment else c.column_name
                                for c in cols[:30])
            lines.append(f"- {t.table_name}（{t.layer.value}，{t.comment or '无注释'}）：{col_str}")
        catalog = "\n".join(lines)

    parts = [f"<目标方言>{body.dialect}</目标方言>", f"<数据目录>\n{catalog or '（无）'}\n</数据目录>"]
    if body.instruction:
        parts.append(f"<指令>{sanitize_question(body.instruction)}</指令>")
    if body.etl_sql:
        parts.append(f"<当前加工 SQL>\n{body.etl_sql[:6000]}\n</当前加工 SQL>")
    if body.ddl:
        parts.append(f"<当前 DDL>\n{body.ddl[:3000]}\n</当前 DDL>")
    if body.mode == "fix":
        parts.append("请修复上述 SQL 的问题并输出完整修复结果。")
    elif body.mode == "explain":
        parts.append("请解释上述 SQL。")
    else:
        parts.append("请输出 SQL：")

    try:
        resp = await gateway.chat(LlmScene.ETL_DESIGN,
                                  [ChatMessage("system", AI_EDIT_SYSTEM),
                                   ChatMessage("user", "\n\n".join(parts))],
                                  temperature=0.1, max_tokens=4096)
        text = resp.text.strip()
    except Exception as e:
        raise ValidationError(f"AI 生成失败：{e.__class__.__name__}（请检查默认 LLM 配置）") from e

    if body.mode in ("generate", "rewrite", "fix"):
        text = re.sub(r"^```(?:sql)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
        w: list[str] = []
        text = etl_skill._strip_dangerous(text, w, "AI 输出")  # 安全剔除
        return {"sql": text, "warnings": w}
    return {"text": text}
