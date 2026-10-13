"""AI 建模工程师（PRD 3.3D-6）：内置编码代理引擎，吸收 OpenAI Codex 核心架构。

代理循环（同 Codex 的 agentic coding loop）：模型推理 → 结构化工具调用（JSON 动作
协议）→ 宿主执行并回喂结果 → 继续推理，直至产出最终工件（建模方案 / 任务修订）。
代理所用 LLM 经 `modeling_agent` 场景绑定自由配置（PRD：代码智能体的模型可配置）。

安全裁剪（PRD 安全边界）：
- 工具白名单只读元数据，无 shell / 网络 / 文件写；唯一"写"是产出方案文本
- 步数上限 MAX_STEPS，超限强制收尾
- 产出经危险语句剔除 + sqlglot 校验 + 数仓规范检查后才允许呈现
- ctx 非空时每步通过 SSE 可见（step 事件），供页面逐步展示与审计
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.skills.etl import _extract_json, _strip_dangerous, sqlglot_check
from app.db.models import DwStandard, EtlTask, MetaColumn, MetaTable
from app.services import dw_standards
from app.db.models.llm import LlmScene
from app.llm.gateway import gateway
from app.llm.types import ChatMessage

logger = logging.getLogger(__name__)

MAX_STEPS = 8

MODE_PROPOSE = "propose"    # 需求 → 结构化建模方案
MODE_REVISE = "revise"      # 任务级对话式修订

_TOOLS_DOC = """可用工具（每轮最多调用一个）：
- search_tables(keyword)：按关键字检索元数据表（匹配表名/中文注释），返回候选表清单
- get_table_schema(table)：查看指定表的字段、类型、注释、分区、分层
- list_layer_tables(layer)：列出某分层（ods/dwd/dws/ads/dim）下已有的表
- get_existing_tasks()：已有建模任务清单（目标表/调度），避免重复建设
- validate_sql(ddl, etl_sql)：sqlglot 语法 + 元数据引用校验，返回错误列表（空=通过）
- get_dw_standards()：已启用的数仓规范全文（命名/分层/分区/注释等），产出必须遵守"""

_PROTOCOL = """你是「AI 建模工程师」，以代理循环方式工作：每轮只输出一个 JSON 对象，二选一，不要输出任何其它文字。
1) 调用工具：
{"action":"tool_call","tool":"工具名","args":{...},"thought":"调用原因"}
2) 最终产出（信息足够时）：
{"action":"final", ...产出字段}

规则：
- 禁止编造元数据中不存在的表/字段；所有来源表必须先用工具确认存在
- 先用工具摸清相关表结构与已有任务，再产出方案；产出前建议用 validate_sql 自检
- 工具结果会以 TOOL_RESULT 形式回喂；最多 """ + str(MAX_STEPS) + """ 步，请收敛
- DDL 一律带 IF NOT EXISTS 与中文 COMMENT；加工 SQL 使用 ${ds} 分区变量；绝不输出 DROP/DELETE/UPDATE/ALTER
- 所有产出必须符合已启用的数仓规范（可用 get_dw_standards 查看；上下文附有规范摘要）"""

_PROPOSE_FINAL = """final 产出字段（建模方案）：
{"action":"final",
 "summary":"整体方案说明（段落，中文）",
 "tables":[{"name":"目标表名（分层前缀_业务域_粒度，全小写）","layer":"dws|ads|dwd|dim",
   "comment":"表中文说明","grain":"粒度，如 天×渠道",
   "ddl":"CREATE TABLE ...","etl_sql":"INSERT OVERWRITE ... ",
   "schedule":"调度建议（人读）","cron":"Azkaban cron（如 0 30 2 * * ?，可空）",
   "source_tables":["来源表名（任务级依赖将由系统按来源表自动匹配，无需输出 depends_on）"],"rationale":"设计理由"}],
 "reuse_notes":"可复用已有表的说明（无则空串）",
 "risks":["风险提示"]}"""

_REVISE_FINAL = """final 产出字段（任务修订）：
{"action":"final",
 "ddl":"修订后的完整 DDL（无需变更则原样输出）",
 "etl_sql":"修订后的完整加工 SQL",
 "change_summary":"修改说明（改了什么、为什么，中文）",
 "notes":"注意事项（可空）"}"""


@dataclass
class AgentScope:
    """代理的受控上下文：DB 会话 + 数据权限范围 + 方言。"""
    db: AsyncSession
    datasource_id: uuid.UUID | None = None
    ds_ids: list[uuid.UUID] | None = None    # None = 不限（管理员）
    dialect: str = "hive"
    _table_cols: dict[str, set[str]] = field(default_factory=dict, repr=False)
    _layer_map: dict[str, str] | None = field(default=None, repr=False)


@dataclass
class AgentResult:
    payload: dict | None = None          # final 产出（已二次校验）
    warnings: list[str] = field(default_factory=list)
    steps_used: int = 0
    error: str = ""                       # 非空表示代理失败/降级
    engine: str = "internal"              # 内置编码代理引擎
    engine_version: str = ""              # 本次代理所用模型（界面与审计展示）


# ---------------- 工具白名单（宿主侧实现） ----------------

async def _scoped_tables(scope: AgentScope):
    q = select(MetaTable).order_by(MetaTable.layer, MetaTable.table_name)
    if scope.datasource_id:
        q = q.where(MetaTable.datasource_id == scope.datasource_id)
    elif scope.ds_ids is not None:
        q = q.where(MetaTable.datasource_id.in_(scope.ds_ids))
    return list((await scope.db.execute(q)).scalars())


async def _tool_search_tables(scope: AgentScope, args: dict) -> dict:
    kw = str(args.get("keyword") or "").lower().strip()
    if not kw:
        return {"error": "keyword 不能为空"}
    hits = [t for t in await _scoped_tables(scope)
            if kw in (t.table_name or "").lower() or (t.comment and kw in t.comment.lower())]
    return {"tables": [{"name": t.table_name, "layer": t.layer.value,
                        "comment": t.comment or ""} for t in hits[:8]],
            "total": len(hits)}


async def _tool_get_table_schema(scope: AgentScope, args: dict) -> dict:
    name = str(args.get("table") or "").strip().lower()
    tables = await _scoped_tables(scope)
    t = next((x for x in tables if (x.table_name or "").lower() == name), None)
    if t is None:
        return {"error": f"表「{name}」不在元数据中（可用 search_tables 检索）"}
    cols = (await scope.db.execute(
        select(MetaColumn).where(MetaColumn.table_id == t.id))).scalars().all()
    return {"name": t.table_name, "layer": t.layer.value, "comment": t.comment or "",
            "partition_cols": t.partition_cols or [],
            "columns": [{"name": c.column_name, "type": c.data_type,
                         "comment": c.comment or ""} for c in cols[:60]]}


async def _tool_list_layer_tables(scope: AgentScope, args: dict) -> dict:
    layer = str(args.get("layer") or "").strip().lower()
    tables = [t for t in await _scoped_tables(scope) if t.layer.value == layer]
    return {"layer": layer, "count": len(tables),
            "tables": [{"name": t.table_name, "comment": t.comment or ""} for t in tables[:30]]}


async def _tool_get_existing_tasks(scope: AgentScope, args: dict) -> dict:
    rows = (await scope.db.execute(select(EtlTask).order_by(EtlTask.updated_at.desc())
                                   .limit(50))).scalars()
    return {"tasks": [{"name": t.name, "target_table": t.target_table, "layer": t.layer,
                       "status": t.status.value} for t in rows]}


async def _tool_validate_sql(scope: AgentScope, args: dict) -> dict:
    if not scope._table_cols:
        for t in await _scoped_tables(scope):
            cols = (await scope.db.execute(
                select(MetaColumn).where(MetaColumn.table_id == t.id))).scalars()
            scope._table_cols[(t.table_name or "").lower()] = {
                (c.column_name or "").lower() for c in cols}
    errors = sqlglot_check(str(args.get("ddl") or ""), str(args.get("etl_sql") or ""),
                           scope.dialect, scope._table_cols)
    return {"ok": not errors, "errors": errors[:10]}


async def _tool_get_dw_standards(scope: AgentScope, args: dict) -> dict:
    standards = await dw_standards.load_enabled(scope.db)
    if not standards:
        return {"standards": [], "note": "当前没有已启用的数仓规范"}
    return {"standards": [{"name": s.name, "content": (s.content or "")[:2000],
                           "rules": [dw_standards.RULE_DEFS.get(r, r)
                                     for r in (s.rules or [])]} for s in standards]}


async def _layer_map(scope: AgentScope) -> dict[str, str]:
    """表名(小写) → 分层，供规范「禁止反向跨层引用」校验。"""
    if scope._layer_map is None:
        scope._layer_map = {(t.table_name or "").lower(): t.layer.value
                            for t in await _scoped_tables(scope)}
    return scope._layer_map


_TOOLS = {
    "search_tables": _tool_search_tables,
    "get_table_schema": _tool_get_table_schema,
    "list_layer_tables": _tool_list_layer_tables,
    "get_existing_tasks": _tool_get_existing_tasks,
    "validate_sql": _tool_validate_sql,
    "get_dw_standards": _tool_get_dw_standards,
}


# ---------------- 产出二次校验 ----------------

def _post_validate(mode: str, payload: dict, scope: AgentScope,
                   warnings: list[str],
                   standards: list[DwStandard] | None = None) -> dict:
    """危险语句剔除 + sqlglot 校验 + 数仓规范检查。产出未过校验不呈现给用户（PRD 安全边界）。"""
    layer_map = scope._layer_map or {}

    def _std_check(name: str, layer: str, ddl: str, sql: str, sources: list) -> None:
        if not standards:
            return
        src_layers = {s: layer_map.get(str(s).lower(), "") for s in (sources or [])}
        warnings.extend(dw_standards.check_rules(
            standards, table_name=name, layer=layer, ddl=ddl, etl_sql=sql,
            source_layers=src_layers))

    if mode == MODE_PROPOSE:
        tables = payload.get("tables")
        if not isinstance(tables, list) or not tables:
            raise ValueError("方案缺少建表清单")
        for tb in tables:
            tb["ddl"] = _strip_dangerous(str(tb.get("ddl") or ""), warnings,
                                         f"表 {tb.get('name', '?')} 的 DDL")
            tb["etl_sql"] = _strip_dangerous(str(tb.get("etl_sql") or ""), warnings,
                                             f"表 {tb.get('name', '?')} 的加工 SQL")
            errs = sqlglot_check(tb["ddl"], tb["etl_sql"], scope.dialect, scope._table_cols)
            if errs:
                warnings.extend(f"表 {tb.get('name', '?')}：{e}" for e in errs[:5])
            _std_check(str(tb.get("name") or ""), str(tb.get("layer") or ""),
                       tb["ddl"], tb["etl_sql"], tb.get("source_tables") or [])
        payload.setdefault("summary", "")
        payload.setdefault("risks", [])
        return payload
    # revise
    payload["ddl"] = _strip_dangerous(str(payload.get("ddl") or ""), warnings, "修订 DDL")
    payload["etl_sql"] = _strip_dangerous(str(payload.get("etl_sql") or ""), warnings,
                                          "修订加工 SQL")
    if not payload["ddl"] and not payload["etl_sql"]:
        raise ValueError("修订结果为空")
    errs = sqlglot_check(payload["ddl"], payload["etl_sql"], scope.dialect, scope._table_cols)
    if errs:
        warnings.extend(errs[:5])
    import re as _re
    m = _re.search(r"create\s+table\s+(?:if\s+not\s+exists\s+)?[`\"'']?([\w.]+)",
                   payload["ddl"], _re.I)
    tname = (m.group(1).split(".")[-1] if m else "")
    _std_check(tname, tname.split("_")[0] if "_" in tname else "",
               payload["ddl"], payload["etl_sql"], [])
    payload.setdefault("change_summary", "")
    return payload


# ---------------- 代理主循环 ----------------

async def run_agent(mode: str, goal: str, scope: AgentScope,
                    history: list[dict] | None = None,
                    extra_context: str = "",
                    ctx=None, override_config_id: str | None = None) -> AgentResult:
    """内置编码代理引擎（PRD 3.3D-6）：JSON 动作协议 + 工具白名单。

    LLM 经 `modeling_agent` 场景绑定解析（可用 override_config_id 指定会话级模型）。
    history: 会话历史 [{role, content, proposal?}]，assistant 消息的 proposal 会
    摘要注入，供多轮修订时理解上一版方案。
    """
    result = AgentResult()
    try:
        resolved = await gateway.resolve_scene(LlmScene.MODELING_AGENT,
                                               override_config_id=override_config_id)
        result.engine_version = resolved.model
    except Exception:
        pass   # 模型名仅作展示，解析失败不阻断（chat 时会给出正式错误）

    # 数仓规范（PRD 3.3E）：注入上下文让代理主动遵守，二次校验再机器核查
    try:
        standards = await dw_standards.load_enabled(scope.db)
    except Exception:
        standards = []
    system = _PROTOCOL + "\n\n" + _TOOLS_DOC + "\n\n" + (
        _PROPOSE_FINAL if mode == MODE_PROPOSE else _REVISE_FINAL)
    std_text = dw_standards.prompt_text(standards)
    if std_text:
        system += "\n\n## 数仓规范（必须遵守）\n" + std_text

    messages: list[ChatMessage] = [ChatMessage("system", system)]
    if extra_context:
        messages.append(ChatMessage("user", f"<上下文>\n{extra_context}\n</上下文>"))
        messages.append(ChatMessage("assistant", "收到，我已了解上下文。"))
    for h in (history or [])[-8:]:
        content = h.get("content") or ""
        if h.get("role") == "assistant" and h.get("proposal"):
            p = h["proposal"]
            names = "、".join(t.get("name", "?") for t in (p.get("tables") or [])[:10])
            content = (content + f"\n（上一版方案包含表：{names}；完整方案用户可见，"
                       "本轮请输出修订后的完整方案并说明差异）")
        messages.append(ChatMessage(h.get("role", "user"), content[:3000]))
    messages.append(ChatMessage("user", goal))

    final: dict | None = None
    for round_i in range(MAX_STEPS):
        st = ctx.step("agent_step", f"第 {round_i + 1} 步") if ctx else None
        try:
            resp = await gateway.chat(LlmScene.MODELING_AGENT, messages,
                                      temperature=0.1, max_tokens=4096,
                                      override_config_id=override_config_id)
        except Exception as e:
            if st:
                st.fail(f"LLM 不可用：{e.__class__.__name__}")
                ctx.emit_step(st)
            result.error = "LLM 不可用，请检查模型配置后重试"
            return result
        action = _extract_json(resp.text)
        if action is None:
            if st:
                st.fail("输出非结构化 JSON")
                ctx.emit_step(st)
            messages.append(ChatMessage("user", "输出无法解析。请只输出一个 JSON 动作对象。"))
            continue

        if action.get("action") == "final":
            try:
                # 确保 validate_sql 的字段缓存已就绪（代理可能没调过 validate_sql）
                if not scope._table_cols:
                    await _tool_validate_sql(scope, {})
                await _layer_map(scope)
                result.payload = _post_validate(mode, action, scope, result.warnings,
                                                standards=standards)
                final = result.payload
                if st:
                    if mode == MODE_PROPOSE:
                        names = "、".join(t.get("name", "?")
                                          for t in (final.get("tables") or [])[:6])
                        st.finish(f"产出方案：{names}")
                    else:
                        st.finish(f"产出修订：{final.get('change_summary', '')[:60]}")
                    ctx.emit_step(st)
            except ValueError as e:
                if st:
                    st.fail(f"产出不合格：{e}")
                    ctx.emit_step(st)
                messages.append(ChatMessage("user", f"final 产出不合格：{e}。请修正后重新 final。"))
                continue
            result.steps_used = round_i + 1
            return result

        if action.get("action") == "tool_call":
            tool = str(action.get("tool") or "")
            fn = _TOOLS.get(tool)
            if fn is None:
                out = {"error": f"未知工具「{tool}」（白名单：{'、'.join(_TOOLS)}）"}
            else:
                try:
                    out = await fn(scope, action.get("args") or {})
                except Exception as e:  # 工具异常不致命，回喂让代理换路
                    logger.warning("建模代理工具 %s 异常: %s", tool, e)
                    out = {"error": f"工具执行失败：{e.__class__.__name__}"}
            thought = str(action.get("thought") or "")[:60]
            summary = _tool_summary(tool, out)
            if st:
                st.finish(f"{thought + '：' if thought else ''}{tool} → {summary}")
                ctx.emit_step(st)
            messages.append(ChatMessage("assistant", resp.text.strip()[:4000]))
            messages.append(ChatMessage("user", "TOOL_RESULT " + _json_dumps(out)))
            continue

        # 未知动作
        if st:
            st.fail("未知动作")
            ctx.emit_step(st)
        messages.append(ChatMessage("user", "action 只能是 tool_call 或 final，请重试。"))

    result.error = f"超过步数上限（{MAX_STEPS} 步），已强制收尾。请换个更具体的描述重试，或分多轮提出。"
    return result


def _json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, default=str)[:3000]


def _tool_summary(tool: str, out: dict) -> str:
    if "error" in out:
        return f"失败（{out['error'][:40]}）"
    if tool == "search_tables":
        return f"{out.get('total', 0)} 张候选表"
    if tool == "get_table_schema":
        return f"{out.get('name')}（{len(out.get('columns', []))} 字段）"
    if tool == "list_layer_tables":
        return f"{out.get('layer')} 层 {out.get('count')} 张表"
    if tool == "get_existing_tasks":
        return f"{len(out.get('tasks', []))} 个已有任务"
    if tool == "validate_sql":
        return "校验通过" if out.get("ok") else f"{len(out.get('errors', []))} 个错误"
    if tool == "get_dw_standards":
        return f"{len(out.get('standards', []))} 条已启用规范"
    return "完成"
