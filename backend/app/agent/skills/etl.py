"""ETL 需求开发助手 Skill（PRD 3.3B）：自然语言加工需求 → 源表推荐 + DDL + 加工 SQL。

安全边界（PRD 强制）：只生成供人工审核/执行的 SQL 文本，本 Skill 绝不连接数仓、
绝不执行任何 DDL/写操作。生成结果入库前经规则校验，危险语句（DROP/DELETE/UPDATE/
TRUNCATE/ALTER 等）会被剔除并显式告警。
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select

from app.agent.skills.base import skill
from app.db.models import KgDimension, KgMetric, MetaColumn, MetaTable, MetricStatus
from app.db.models.llm import LlmScene
from app.kg import service as kg_service
from app.llm.gateway import gateway
from app.llm.types import ChatMessage

logger = logging.getLogger(__name__)

LAYER_NAMES = ("ods", "dwd", "dws", "ads", "dim")
# 加工 SQL 只允许 CREATE TABLE / INSERT (OVERWRITE|INTO) ... SELECT；
# 这些关键字出现即判危险，整段剔除并告警。
_FORBIDDEN_RE = re.compile(
    r"\b(drop|truncate|delete\s+from|delete\b|update\s+\w+\s+set|alter\s+table|"
    r"grant|revoke|create\s+(or\s+replace\s+)?(view|function|procedure)|merge\s+into)\b",
    re.I)

ETL_SYSTEM = """你是资深数据仓库开发工程师。根据用户的加工需求和给出的候选源表元数据，产出一份 ETL 设计方案。
严格规则：
1. 只能使用「候选源表」中列出的表和字段作为来源，禁止编造表名、字段名。
2. 输出必须是严格 JSON（不要 markdown 代码块、不要任何额外文字），结构：
{
  "target_table": "目标表名（分层前缀_业务域_粒度，如 dws_trade_daily）",
  "layer": "dws|ads|dwd|dim 之一",
  "schedule": "调度建议，如 每日凌晨 T+1",
  "ddl": "CREATE TABLE 语句，含字段注释、分区定义（分区字段用 dt，字符串类型）",
  "etl_sql": "INSERT OVERWRITE/INTO 加工语句，分区值用 ${ds} 变量，含 WHERE 分区裁剪与 GROUP BY",
  "field_mapping": [{"target": "目标字段", "source": "源表.源字段", "rule": "加工逻辑，如 SUM/原样"}],
  "notes": "口径说明与注意事项（一段文字）"
}
3. DDL 与加工 SQL 使用目标方言语法；聚合口径必须与 KG 指标口径一致。
4. 粒度-聚合匹配：按天汇总就要 GROUP BY 到天；比率类指标不要对百分比直接求和。
5. 不确定的字段映射在 rule 里写「待确认」，不要编造。"""


@dataclass
class EtlDesign:
    target_table: str = ""
    layer: str = ""
    schedule: str = ""
    ddl: str = ""
    etl_sql: str = ""
    dialect: str = ""
    source_tables: list[dict] = field(default_factory=list)  # {table, layer, reason}
    field_mapping: list[dict] = field(default_factory=list)  # {target, source, rule}
    notes: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"target_table": self.target_table, "layer": self.layer,
                "schedule": self.schedule, "ddl": self.ddl, "etl_sql": self.etl_sql,
                "dialect": self.dialect,
                "source_tables": self.source_tables, "field_mapping": self.field_mapping,
                "notes": self.notes, "warnings": self.warnings}

    def to_markdown(self) -> str:
        p: list[str] = []
        if self.target_table:
            p.append(f"**目标表**：`{self.target_table}`"
                     + (f"（{self.layer.upper()} 层）" if self.layer else "")
                     + (f"　**调度建议**：{self.schedule}" if self.schedule else ""))
        if self.source_tables:
            p.append("\n**推荐源表**：")
            for s in self.source_tables:
                p.append(f"- `{s['table']}`（{s.get('layer', '?')} 层）—— {s.get('reason', '')}")
        if self.ddl:
            p.append(f"\n**建表语句（DDL）**：\n```sql\n{self.ddl}\n```")
        if self.etl_sql:
            p.append(f"\n**加工 SQL**：\n```sql\n{self.etl_sql}\n```")
        if self.field_mapping:
            p.append("\n**字段映射**：")
            for fm in self.field_mapping[:20]:
                p.append(f"- `{fm.get('target', '?')}` ⇐ `{fm.get('source', '?')}`"
                         + (f"（{fm['rule']}）" if fm.get("rule") else ""))
        if self.notes:
            p.append(f"\n**口径说明**：{self.notes}")
        if self.warnings:
            p.append("\n**⚠ 规范校验提示**：")
            for w in self.warnings:
                p.append(f"- {w}")
        p.append("\n> 以上 SQL 仅供人工审核后在数仓执行，系统不会自动执行任何 DDL/写操作。")
        return "\n".join(p)


async def _recommend_sources(session, question: str, datasource_id) -> list[dict]:
    """源表推荐：KG 指标/维度反查 + 表名/注释模糊命中，附推荐理由。"""
    out: dict[str, dict] = {}
    linked = await kg_service.link_entities(session, question, datasource_id)
    metric_names: dict[str, list[str]] = {}
    for c in linked.metrics[:8]:
        m = await session.get(KgMetric, uuid.UUID(c.id))
        if m and m.table_id:
            metric_names.setdefault(str(m.table_id), []).append(m.name)
    dim_tables: dict[str, list[str]] = {}
    for c in linked.dimensions[:8]:
        d = await session.get(KgDimension, uuid.UUID(c.id))
        for rel in (d.related_columns or []) if d else []:
            if rel.get("table_id"):
                dim_tables.setdefault(rel["table_id"], []).append(d.name)

    cand_ids = set(metric_names) | set(dim_tables)
    if cand_ids:
        cands = await kg_service.resolve_tables(
            session,
            [str(m) for m in [c.id for c in linked.metrics[:8]]],
            [c.id for c in linked.dimensions[:8]])
        score = {c.table_id: c.score for c in cands}
        for tid in cand_ids:
            t = await session.get(MetaTable, uuid.UUID(tid))
            if t is None:
                continue
            reasons = []
            if metric_names.get(tid):
                reasons.append("覆盖指标 " + "、".join(metric_names[tid][:4]))
            if dim_tables.get(tid):
                reasons.append("含维度 " + "、".join(sorted(set(dim_tables[tid]))[:4]))
            out[tid] = {"table": t.table_name, "layer": t.layer.value,
                        "reason": "；".join(reasons) or "实体链接命中",
                        "_score": score.get(tid, 0)}

    # 表名/注释模糊命中兜底
    q = question.lower()
    for t in (await session.execute(
            select(MetaTable).where(MetaTable.datasource_id == datasource_id))).scalars():
        tid = str(t.id)
        if tid in out:
            continue
        if (t.table_name or "").lower() in q or (
                t.comment and any(tok in q for tok in re.split(r"[^一-鿿0-9a-zA-Z]+", t.comment.lower())
                                  if len(tok) >= 2)):
            out[tid] = {"table": t.table_name, "layer": t.layer.value,
                        "reason": "表名/注释与需求相关", "_score": 1}

    ranked = sorted(out.values(), key=lambda x: -x.pop("_score"))
    return ranked[:6]


async def _source_context(session, sources: list[dict], datasource_id) -> str:
    """候选源表字段清单，注入 LLM 提示词。"""
    if not sources:
        return "（无候选源表元数据）"
    names = [s["table"] for s in sources]
    tables = (await session.execute(
        select(MetaTable).where(MetaTable.datasource_id == datasource_id,
                                MetaTable.table_name.in_(names)))).scalars().all()
    parts: list[str] = []
    for t in tables:
        cols = (await session.execute(
            select(MetaColumn).where(MetaColumn.table_id == t.id))).scalars().all()
        head = f"### {t.table_name}（{t.layer.value} 层，{t.comment or '暂无注释'}"
        if t.partition_cols:
            head += f"，分区: {','.join(t.partition_cols[:3])}"
        head += "）"
        lines = [head]
        for c in cols[:50]:
            ln = f"  {c.column_name} {c.data_type}"
            if c.comment:
                ln += f" -- {c.comment}"
            lines.append(ln)
        parts.append("\n".join(lines))
    # KG 指标口径（供聚合对齐）
    mets = (await session.execute(
        select(KgMetric).where(KgMetric.status == MetricStatus.ACTIVE,
                               KgMetric.table_id.in_([t.id for t in tables])))).scalars().all()
    if mets:
        parts.append("\n### 相关指标口径（聚合方式必须遵守）")
        for m in mets[:15]:
            parts.append(f"- {m.name}：{m.business_def or '未登记'}"
                         + (f"；聚合={m.agg_function}" if m.agg_function else ""))
    return "\n\n".join(parts)


def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except (ValueError, json.JSONDecodeError):
        return None


def _strip_dangerous(sql: str, warnings: list[str], label: str) -> str:
    """剔除危险语句（分号切分后逐句检查）。绝不输出 DROP/DELETE/UPDATE 等写操作。"""
    if not sql:
        return ""
    kept, dropped = [], 0
    for stmt in sql.split(";"):
        if _FORBIDDEN_RE.search(stmt):
            dropped += 1
        elif stmt.strip():
            kept.append(stmt)
    if dropped:
        warnings.append(f"{label}中检出 {dropped} 条危险语句（DROP/DELETE/UPDATE/ALTER 等），已剔除；"
                        "系统只生成只读加工脚本，绝不执行写操作")
    return ";".join(kept).strip().strip(";") + (";" if kept else "")


def validate_design(design: EtlDesign, existing_names: set[str], dialect: str,
                    question: str) -> None:
    """规则化规范校验（不依赖 LLM）：命名/重名/粒度-聚合/分区/安全。"""
    w = design.warnings
    name = design.target_table

    # 1) 命名规范：分层前缀
    if name:
        if not re.match(r"^(ods|dwd|dws|ads|dim)_[a-z0-9_]+$", name):
            w.append(f"目标表名「{name}」不符合命名规范（应为 分层前缀_业务域_粒度，"
                     "如 dws_trade_daily，全小写下划线）")
        # 分层一致性：DDL 自称的分层与名字前缀不一致
        if design.layer and not name.startswith(design.layer + "_"):
            w.append(f"表名前缀与所选分层 {design.layer.upper()} 不一致")
    else:
        w.append("未识别到目标表名，请人工补充")

    # 2) 重名检测
    if name and name in existing_names:
        w.append(f"库中已存在同名表「{name}」，请确认是覆盖改造还是改名新建")

    # 3) 粒度-聚合匹配
    if design.etl_sql:
        sql_l = design.etl_sql.lower()
        has_agg = bool(re.search(r"\b(sum|count|avg|max|min)\s*\(", sql_l))
        has_gb = "group by" in sql_l
        if has_agg and not has_gb:
            w.append("加工 SQL 含聚合函数但没有 GROUP BY，粒度可能不正确，请人工核对")
        daily_words = {"按天", "每日", "天级", "日粒度", "每天", "日报"}
        if daily_words & set(re.findall(r"按天|每日|天级|日粒度|每天|日报", question)) \
                and has_gb and "dt" not in sql_l and "date" not in sql_l:
            w.append("需求为天级粒度，但加工 SQL 的分组里未看到日期字段（dt/date），请核对粒度")

    # 4) 分区建议（Hive/Spark 类引擎强依赖分区裁剪）
    if dialect in ("hive", "spark", "doris", "starrocks") and design.etl_sql:
        if "${ds}" not in design.etl_sql and "${ ds }" not in design.etl_sql:
            w.append("加工 SQL 未使用分区变量 ${ds}，建议按分区写入以避免全量刷新")

    # 5) 安全：剔除危险语句
    design.ddl = _strip_dangerous(design.ddl, w, "DDL")
    design.etl_sql = _strip_dangerous(design.etl_sql, w, "加工 SQL")

    # 6) DDL 必须含 CREATE TABLE
    if design.ddl and not re.search(r"\bcreate\s+table\b", design.ddl, re.I):
        w.append("DDL 中未找到 CREATE TABLE 语句，请人工核对")
    if not design.ddl and not w:
        w.append("DDL 生成失败，请补充需求细节后重试")


# ---------- 子 Agent：规划 → 起草 → 校验 → 修复（PRD 3.3B，SSE 子步骤可见） ----------

PLAN_SYSTEM = """你是数据仓库需求分析师。把用户的加工需求解析为结构化计划，只输出严格 JSON：
{
  "target_table": "目标表名（分层前缀_业务域_粒度，如 dws_trade_daily，全小写）",
  "layer": "dws|ads|dwd|dim 之一",
  "grain": "粒度描述，如 天×渠道",
  "metrics": ["需要的指标名"],
  "dimensions": ["需要的维度名"],
  "schedule": "调度建议，如 每日凌晨 T+1"
}
不确定时给出最合理推断，禁止编造指标名之外的业务口径。"""

# sqlglot 方言映射（doris/starrocks 走 mysql 协议语法）
_SQLGLOT_DIALECT = {"mysql": "mysql", "doris": "mysql", "starrocks": "mysql",
                    "hive": "hive", "spark": "spark", "clickhouse": "clickhouse",
                    "sqlite": "sqlite", "postgres": "postgres"}
FIX_MAX_ROUNDS = 2


def sqlglot_check(ddl: str, etl_sql: str, dialect: str,
                  table_columns: dict[str, set[str]]) -> list[str]:
    """sqlglot 语法解析 + 元数据引用校验（表/字段存在性）。返回错误列表（空=通过）。

    - 语法：解析失败 或 解析结果不是预期的语句类型（SELECT/INSERT/CREATE/WITH/UNION）即报错
      （sqlglot 对乱码很宽容，会把「SELEC 错误语法」解析成 Alias，必须卡语句类型）。
    - 引用：FROM/JOIN 的表必须存在于元数据；别名会解析回真实表名再校验字段。
      未提供元数据（table_columns 为空）时跳过引用校验。
    """
    import sqlglot
    from sqlglot import exp
    errors: list[str] = []
    read_dialect = _SQLGLOT_DIALECT.get(dialect, "mysql")
    allowed = (exp.Insert, exp.Select, exp.Union, exp.With, exp.Create)

    def parse_one(sql: str, label: str) -> list:
        if not sql.strip():
            return []
        try:
            stmts = [s for s in sqlglot.parse(sql, read=read_dialect) if s is not None]
        except Exception as e:
            errors.append(f"{label} 语法解析失败: {str(e)[:150]}")
            return []
        for s in stmts:
            if not isinstance(s, allowed):
                errors.append(f"{label} 无法识别为有效 SQL 语句（解析为 {type(s).__name__}），"
                              "请检查语法")
        return stmts

    parse_one(ddl, "DDL")
    stmts = parse_one(etl_sql, "加工 SQL")

    if not table_columns:
        return errors
    known = {k.lower(): {c.lower() for c in v} for k, v in table_columns.items()}
    target = design_target_lower(ddl)
    for st in stmts:
        # INSERT 写入的目标表不参与「源表存在性」校验
        if isinstance(st, exp.Insert) and isinstance(st.this, exp.Table):
            target = target or (st.this.name or "").lower()
        # 别名 → 真实表名
        alias_map: dict[str, str] = {}
        for tbl in st.find_all(exp.Table):
            tname = (tbl.name or "").lower()
            if tbl.alias:
                alias_map[tbl.alias.lower()] = tname
            if not tname or tname == target:
                continue
            if tname not in known:
                errors.append(f"加工 SQL 引用了元数据中不存在的表「{tbl.name}」")
        for col in st.find_all(exp.Column):
            if col.table:
                real = alias_map.get(col.table.lower(), col.table.lower())
                if real in known and col.name.lower() not in known[real]:
                    errors.append(f"字段 {col.table}.{col.name} 在表 {real} 的元数据中不存在")
                elif real != target and real not in known:
                    errors.append(f"字段 {col.table}.{col.name} 所在表「{real}」未在元数据登记，无法校验")
    return errors


def design_target_lower(ddl: str) -> str:
    m = re.search(r"create\s+table\s+(?:if\s+not\s+exists\s+)?([\w.]+)", ddl or "", re.I)
    return (m.group(1).split(".")[-1].lower() if m else "")


async def _llm_json(scene, system: str, user: str, override_config_id: str | None,
                    max_tokens: int = 4096) -> dict | None:
    resp = await gateway.chat(scene, [ChatMessage("system", system), ChatMessage("user", user)],
                              temperature=0.1, max_tokens=max_tokens,
                              override_config_id=override_config_id)
    data = _extract_json(resp.text)
    if data is None:
        logger.warning("ETL %s 输出未解析为 JSON（前 200 字符）: %s",
                       getattr(scene, "value", scene), (resp.text or "")[:200])
    return data


def _apply_draft(design: EtlDesign, data: dict) -> None:
    design.target_table = str(data.get("target_table") or design.target_table or "").strip()
    layer = str(data.get("layer") or "").strip().lower()
    if layer in LAYER_NAMES:
        design.layer = layer
    design.schedule = str(data.get("schedule") or design.schedule or "").strip()
    design.ddl = str(data.get("ddl") or "").strip()
    design.etl_sql = str(data.get("etl_sql") or "").strip()
    if isinstance(data.get("field_mapping"), list):
        design.field_mapping = [
            {"target": str(fm.get("target", "")), "source": str(fm.get("source", "")),
             "rule": str(fm.get("rule", ""))}
            for fm in data["field_mapping"] if isinstance(fm, dict)][:30]
    design.notes = str(data.get("notes") or "").strip()


async def _table_columns(session, datasource_id, extra_names: list[str]) -> dict[str, set[str]]:
    """{表名: 字段集合}，供 sqlglot 引用校验。"""
    tables = (await session.execute(
        select(MetaTable).where(MetaTable.datasource_id == datasource_id))).scalars().all()
    out: dict[str, set[str]] = {}
    for t in tables:
        cols = (await session.execute(
            select(MetaColumn.column_name).where(MetaColumn.table_id == t.id))).scalars().all()
        out[t.table_name] = set(cols)
    return out


@skill("etl_design", "基于元数据的 ETL 需求开发子 Agent（仅生成，不执行）")
async def etl_design(*, session, question: str, datasource,
                     override_config_id: str | None = None, ctx=None) -> EtlDesign:
    """子 Agent 循环：源表推荐 → 需求规划 → 起草 DDL/SQL → 校验 → 错误回喂修复。

    ctx：可选的管线上下文（提供则发射 etl_plan/etl_draft/etl_check/etl_fix 子步骤 SSE）。
    """
    dialect = datasource.db_type.value
    design = EtlDesign(dialect=dialect)

    def step(name: str):
        return ctx.step(name) if ctx is not None else None

    def emit(st, detail: str = ""):
        if ctx is not None and st is not None:
            ctx.emit_step(st, detail)

    # ---- 0. 源表推荐（KG 反查，不依赖 LLM） ----
    design.source_tables = await _recommend_sources(session, question, datasource.id)
    src_ctx = await _source_context(session, design.source_tables, datasource.id)
    existing = set((await session.execute(
        select(MetaTable.table_name).where(MetaTable.datasource_id == datasource.id)
    )).scalars())
    table_cols = await _table_columns(session, datasource.id, [])

    # ---- 1. 规划：需求 → 目标表/粒度/指标/维度 ----
    st = step("etl_plan")
    plan: dict | None = None
    try:
        plan = await _llm_json(LlmScene.ETL_DESIGN, PLAN_SYSTEM,
                               f"<加工需求>{question[:800]}</加工需求>\n"
                               f"<已有表（避免重名）>{'、'.join(sorted(existing))[:1500] or '无'}</已有表>\n"
                               "请输出计划 JSON：",
                               override_config_id, max_tokens=800)
        if plan:
            design.target_table = str(plan.get("target_table") or "").strip()
            layer = str(plan.get("layer") or "").strip().lower()
            design.layer = layer if layer in LAYER_NAMES else ""
            design.schedule = str(plan.get("schedule") or "").strip()
        if st:
            st.finish(f"目标表 {design.target_table or '未定'}"
                      + (f"，粒度 {plan.get('grain')}" if plan and plan.get('grain') else ""))
    except Exception as e:
        if st:
            st.fail(f"LLM 不可用: {e.__class__.__name__}")
    emit(st)

    # ---- 2. 起草：DDL + 加工 SQL（带修复循环） ----
    st = step("etl_draft")
    draft: dict | None = None
    last_errors: list[str] = []
    llm_ok = True
    for round_i in range(FIX_MAX_ROUNDS + 1):
        fix_part = ""
        if last_errors:
            fix_part = ("\n\n<上一稿的问题>\n" + "\n".join(f"- {e}" for e in last_errors[:8])
                        + "\n</上一稿的问题>\n请修正后重新输出完整 JSON。")
        user = (f"<目标方言>{dialect}</目标方言>\n\n<候选源表>\n{src_ctx}\n</候选源表>\n\n"
                f"<加工需求>{question[:800]}</加工需求>\n"
                + (f"<已确定的目标表>{design.target_table}（{design.layer} 层）</已确定的目标表>\n"
                   if design.target_table else "")
                + fix_part + "\n请输出 ETL 设计方案 JSON：")
        try:
            draft = await _llm_json(LlmScene.ETL_DESIGN, ETL_SYSTEM, user, override_config_id)
        except Exception as e:
            llm_ok = False
            design.warnings.append(f"LLM 生成失败（{e.__class__.__name__}），"
                                   "以下仅给出源表推荐，DDL/加工 SQL 请人工编写")
            break
        if not draft:
            last_errors = ["LLM 输出无法解析为 JSON，请严格按结构输出"]
            continue
        _apply_draft(design, draft)
        # 校验（语法 + 引用 + 规范），有错则回喂修复。
        # 先做危险语句剔除再校验：LLM 常附带 DROP TABLE IF EXISTS 之类的预清理语句，
        # 反正会被安全规则剔除，不该为它们浪费修复轮次。
        clean_ddl = _strip_dangerous(design.ddl, [], "DDL")
        clean_sql = _strip_dangerous(design.etl_sql, [], "加工 SQL")
        check_errors = sqlglot_check(clean_ddl, clean_sql, dialect, table_cols)
        if not check_errors:
            break
        last_errors = check_errors
        if st and round_i < FIX_MAX_ROUNDS:
            # 修复轮次单独可见
            fst = step("etl_fix")
            fst.finish(f"第 {round_i + 1} 轮修复：{check_errors[0][:60]}")
            emit(fst)
    else:
        if last_errors and draft:
            design.warnings.append("自动修复后仍有待核对项：" + "；".join(last_errors[:4]))
    if llm_ok and draft is None and not any("LLM" in w for w in design.warnings):
        design.warnings.append("LLM 输出无法解析为结构化方案，请重试或补充需求细节")

    if st:
        if not llm_ok:
            st.fail("LLM 不可用")
        elif design.etl_sql:
            # design 保留最近一次成功起草的内容（修复轮解析失败不清空旧稿）
            st.finish(f"DDL {len(design.ddl)} 字符 / 加工 SQL {len(design.etl_sql)} 字符")
        else:
            st.fail("未产出可用 SQL")
    emit(st)

    # ---- 3. 规范校验（规则化，含安全剔除） ----
    st = step("etl_check")
    validate_design(design, existing, dialect, question)
    if st:
        if design.warnings:
            st.finish(f"{len(design.warnings)} 条提示")
        else:
            st.finish("通过")
    emit(st)
    return design
