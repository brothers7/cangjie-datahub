"""AI 数据治理规则引擎（PRD 3.3F）：六域巡检，产出结构化治理项。

治理域：metadata 元数据 / model 模型 / schedule 任务调度 / quality 数据质量 /
security 安全合规 / cost 成本性能。
规则引擎先行（无 LLM 也有结论）；LLM 报告在 API 层增强。
每个治理项：{domain, level(info/warn/block), object, title, evidence, suggestion,
action(none/new_task/revise_task), task_id?}
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (DwStandard, EdgeType, EtlTask, KgEdge, MaskingRule, MetaColumn,
                           MetaTable, TableLayer)
from app.services import dw_standards

DOMAIN_LABELS = {
    "metadata": "元数据治理", "model": "模型治理", "schedule": "任务与调度治理",
    "quality": "数据质量治理", "security": "安全与合规治理", "cost": "成本与性能治理",
}

# 疑似敏感字段命名/注释模式（手机号/身份证/邮箱/姓名/银行卡）
_SENSITIVE_RE = re.compile(
    r"phone|mobile|(?<![a-z])tel(?![a-z])|id_?card|idcard|identity|e-?mail|bank_?card|card_?no|"
    r"身份证|手机|电话|邮箱|银行卡|姓名", re.I)


def _finding(domain: str, level: str, obj: str, title: str, evidence: str,
             suggestion: str, action: str = "none", task_id: str = "") -> dict:
    return {"domain": domain, "level": level, "object": obj, "title": title,
            "evidence": evidence, "suggestion": suggestion,
            "action": action, "task_id": task_id}


async def run_rule_engine(db: AsyncSession, ds_ids: list | None) -> list[dict]:
    """执行全部规则检查，返回治理项列表（按级别 block > warn > info 排序）。"""
    findings: list[dict] = []

    tq = select(MetaTable)
    if ds_ids is not None:
        tq = tq.where(MetaTable.datasource_id.in_(ds_ids))
    tables = list((await db.execute(tq)).scalars())
    tname = {t.table_name: t for t in tables}
    layer_of = {(t.table_name or "").lower(): t.layer.value for t in tables}

    tasks = list((await db.execute(select(EtlTask))).scalars())
    # 分层索引：元数据表 + 任务目标表（未入元数据的在建表也能参与跨层判断）
    for task in tasks:
        if task.target_table and task.layer:
            layer_of.setdefault(task.target_table.lower(), task.layer.lower())

    # ---------- 元数据治理 ----------
    for t in tables:
        if not t.comment:
            findings.append(_finding(
                "metadata", "warn", t.table_name, "表缺少注释",
                f"{t.layer.value.upper()} 层表 {t.table_name} 无中文注释",
                "补充表注释，提升库表答疑与 AI 建模准确性", "none"))
    if tables:
        col_q = select(MetaColumn).where(MetaColumn.table_id.in_([t.id for t in tables]))
        cols = list((await db.execute(col_q)).scalars())
        tid2name = {t.id: t.table_name for t in tables}
        no_comment: dict[str, int] = {}
        total: dict[str, int] = {}
        for c in cols:
            n = tid2name.get(c.table_id, "?")
            total[n] = total.get(n, 0) + 1
            if not c.comment:
                no_comment[n] = no_comment.get(n, 0) + 1
        worst = sorted(((n, c, total[n]) for n, c in no_comment.items() if total[n] >= 3),
                       key=lambda x: -x[1] / x[2])[:5]
        for n, c, tot in worst:
            if c / tot >= 0.8:
                findings.append(_finding(
                    "metadata", "warn", n, "字段注释覆盖率过低",
                    f"表 {n} 共 {tot} 个字段，{c} 个无注释（{round(c / tot * 100)}%）",
                    "批量补齐核心字段中文注释（可在数据源详情或元数据扫描后编辑）", "none"))
    # 命名不符合分层前缀（启用规范时升级为规范违规）
    standards = await dw_standards.load_enabled(db)
    if any("layer_prefix_required" in (s.rules or []) for s in standards):
        for t in tables:
            if not (t.table_name or "").lower().startswith(
                    ("ods_", "dwd_", "dws_", "ads_", "dim_")):
                findings.append(_finding(
                    "metadata", "warn", t.table_name, "命名不符合分层规范",
                    f"表名 {t.table_name} 未带分层前缀（登记分层 {t.layer.value}）",
                    "按数仓规范重命名或迁表，命名规范见「数仓规范」模块", "none"))

    # ---------- 模型治理 ----------
    connected: set = set()
    table_ids = [t.id for t in tables]
    if table_ids:
        for e in (await db.execute(
                select(KgEdge).where(KgEdge.edge_type == EdgeType.TABLE_JOIN,
                                     KgEdge.src_id.in_(table_ids)))).scalars():
            connected.add(e.src_id)
            connected.add(e.dst_id)
    referenced: set[str] = set()
    target_count: dict[str, list[EtlTask]] = {}
    for task in tasks:
        if task.target_table:
            target_count.setdefault(task.target_table, []).append(task)
        for s in (task.source_tables or []):
            if isinstance(s, dict) and s.get("table"):
                referenced.add(s["table"])
    for t in tables:
        if t.id not in connected and t.layer not in (TableLayer.ODS, TableLayer.DIM) \
                and t.table_name not in referenced:
            findings.append(_finding(
                "model", "info", t.table_name, "孤岛表（无上下游关系）",
                f"{t.table_name} 无任何 JOIN 关系登记，也不被任何任务引用",
                "确认是否废弃；若在用请补充表关系登记，否则建议下线以减少认知负担",
                "none"))
    for t in tables:
        if t.layer in (TableLayer.DWS, TableLayer.ADS) and t.table_name not in target_count:
            findings.append(_finding(
                "model", "warn", t.table_name, "有表无加工任务",
                f"{t.layer.value.upper()} 表 {t.table_name} 没有任何任务把它作为目标表",
                "为该表新建加工任务并配置调度，否则数据不会更新", "new_task"))
    for target, ts in target_count.items():
        if len(ts) > 1:
            findings.append(_finding(
                "model", "warn", target, "多任务写同目标表（重复加工风险）",
                f"任务 {'、'.join(t.name for t in ts[:4])} 都写入 {target}",
                "合并为一个任务或明确分区/职责边界，避免互相覆盖", "revise_task",
                str(ts[0].id)))
    # 跨层异常：任务目标层 < 某个来源层（反向引用）
    for task in tasks:
        tgt_layer = (task.layer or "").lower()
        for s in (task.source_tables or []):
            if not isinstance(s, dict):
                continue
            src_layer = layer_of.get(str(s.get("table", "")).lower(), "")
            order = dw_standards._LAYER_ORDER
            if order.get(src_layer, -1) > order.get(tgt_layer, 99) and tgt_layer in order:
                findings.append(_finding(
                    "model", "block", task.target_table or task.name, "反向跨层引用",
                    f"任务「{task.name}」目标层 {tgt_layer} 却引用更高层来源表 "
                    f"{s.get('table')}（{src_layer}）",
                    "调整分层归属或拆解任务，保持 ods→dwd→dws→ads 单向流动",
                    "revise_task", str(task.id)))

    # ---------- 任务与调度治理 ----------
    for task in tasks:
        if task.status.value in ("draft", "validated") and not task.cron:
            findings.append(_finding(
                "schedule", "info", task.name, "任务未配置调度",
                f"任务「{task.name}」（{task.target_table}）无 cron，不会自动执行",
                "补充 cron 调度表达式并推送 Azkaban", "revise_task", str(task.id)))
        if task.depends_on:
            # depends_on 语义为任务 id 列表（PRD 3.3G）
            id_set = {str(t2.id) for t2 in tasks if t2.id != task.id}
            missing = [d for d in (str(x) for x in task.depends_on) if d not in id_set]
            if missing:
                findings.append(_finding(
                    "schedule", "warn", task.name, "依赖的任务不存在",
                    f"任务「{task.name}」依赖的 {len(missing)} 个任务已不存在，实例会一直等待依赖",
                    "在任务详情调整依赖任务（移除失效依赖）",
                    "revise_task", str(task.id)))

    # ---------- 数据质量治理（静态部分；动态探查走 /governance/probe） ----------
    for task in tasks:
        sql = task.etl_sql or ""
        if not sql.strip():
            findings.append(_finding(
                "quality", "warn", task.name, "加工 SQL 为空",
                f"任务「{task.name}」没有加工 SQL，目标表 {task.target_table} 不会有数据",
                "补写加工 SQL 或废弃该任务", "revise_task", str(task.id)))

    # ---------- 安全与合规治理 ----------
    mask_q = select(MaskingRule).where(MaskingRule.enabled.is_(True))
    mask_rules = list((await db.execute(mask_q)).scalars())
    if tables:
        cols2 = list((await db.execute(
            select(MetaColumn).where(MetaColumn.table_id.in_(table_ids)))).scalars())
        tid2ds = {t.id: t.datasource_id for t in tables}
        sensitive_hits: dict[str, list[str]] = {}
        for c in cols2:
            text = f"{c.column_name} {c.comment or ''}"
            if not _SENSITIVE_RE.search(text):
                continue
            ds_id = tid2ds.get(c.table_id)
            covered = any(m.datasource_id == ds_id and _pattern_match(
                m.column_pattern, c.column_name) for m in mask_rules)
            if not covered:
                tn = tid2name.get(c.table_id, "?")
                sensitive_hits.setdefault(tn, []).append(c.column_name)
        for tn, col_list in list(sensitive_hits.items())[:8]:
            findings.append(_finding(
                "security", "block", tn, "疑似敏感字段未配置脱敏",
                f"表 {tn} 的字段 {'、'.join(col_list[:5])} 疑似敏感（命名/注释命中），"
                "但数据源未配置覆盖的脱敏规则",
                "在「数据权限」为该数据源添加脱敏规则（列名模式匹配）", "none"))

    # ---------- 成本与性能治理 ----------
    for task in tasks:
        sql = (task.etl_sql or "").strip()
        if not sql:
            continue
        sql_l = sql.lower()
        if re.search(r"select\s+\*", sql_l):
            findings.append(_finding(
                "cost", "warn", task.name, "SELECT * 全列读取",
                f"任务「{task.name}」加工 SQL 使用 SELECT *，列裁剪缺失",
                "改为显式列清单，减少 IO 并防止上游加列导致schema漂移",
                "revise_task", str(task.id)))
        if "${ds}" not in sql and not re.search(r"\bds\s*=", sql_l) \
                and not re.search(r"\bdt\s*=", sql_l) and "limit" not in sql_l:
            findings.append(_finding(
                "cost", "warn", task.name, "疑似全表扫描（无分区裁剪）",
                f"任务「{task.name}」加工 SQL 未发现分区过滤（${{ds}}/ds=/dt=）",
                "为来源表加分区裁剪条件，避免每次全量扫描", "revise_task", str(task.id)))
        if "cross join" in sql_l:
            findings.append(_finding(
                "cost", "block", task.name, "笛卡尔积 JOIN",
                f"任务「{task.name}」存在 CROSS JOIN，数据量有爆炸风险",
                "确认关联条件，改为 INNER/LEFT JOIN", "revise_task", str(task.id)))

    rank = {"block": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: (rank.get(f["level"], 3), f["domain"]))
    return findings


def _pattern_match(pattern: str, column: str) -> bool:
    """脱敏规则的 * 通配匹配。"""
    regex = "^" + re.escape(pattern).replace(r"\*", ".*") + "$"
    return bool(re.match(regex, column, re.I))


REPORT_PROMPT = """你是资深数据治理专家。以下是数仓巡检发现的治理项（JSON），请输出一份治理报告：
1. 按治理域组织（元数据/模型/任务调度/数据质量/安全合规/成本性能），每域先给结论再列重点问题
2. 标注优先级：先处理 block 级（安全/反向跨层/笛卡尔积），再 warn，再 info
3. 对「可优化的任务」给出具体优化方向（如补分区裁剪/显式列清单/合并重复加工）
4. 对「数据质量问题」给出解决建议（如补默认值/加去重/修调度）
5. 结尾给出一周内的落地行动清单（不超过 5 条）
简明、可落地，中文输出，不超过 800 字。

治理项：
{findings}"""
