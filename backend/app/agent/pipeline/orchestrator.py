"""问答主管线编排（PRD 2.3）：全链路步骤与 SSE 事件源。

步骤：落库 → 缓存快查 → 改写 → 意图路由 → 记忆 → 实体链接(可中断确认) →
口径校验 → 表路由 → SQL生成 → 安全校验(修复循环) → 执行 → 校验计算 →
异常检测 → 图表 → (联网) → 流式解读 → 报表 → 回写记忆
"""
from __future__ import annotations

import asyncio
import logging
import re

from sqlalchemy import select

from app.agent import router_agent
from app.agent.memory import long_term, short_term
from app.agent.nlu import entity_linker
from app.agent.nlu.time_parser import MOM_WORDS, YOY_WORDS
from app.agent.pipeline.context import PipelineContext
from app.agent.retrieval import tavily
from app.agent.skills import compute, sql_gen
from app.agent.skills.anomaly import detect_anomalies, find_missing_dates
from app.agent.skills.chart import _resolve_metric_cols, build_chart_spec
from app.agent.skills.compute import _num
from app.agent.skills.report import report_generate
from app.agent.skills.sql_guard import guard_sql
from app.agent.skills.validate import validate_result
from app.connectors.registry import get_connector
from app.core.config import settings
from app.core.errors import AppError, LLMError
from app.db.models import (AuditLog, Conversation, CustomSkill, DataSource, DatasourcePermission,
                           MaskingRule, Message, MetaColumn, MetaTable, QueryRecord, QueryStatus,
                           Report, Role, SkillStatus, SkillType, User)
from app.db.models.base import utcnow
from app.db.session import get_session_factory
from app.kg import service as kg_service
from app.llm.types import ChatMessage

logger = logging.getLogger(__name__)

CONFIRM_THRESHOLD = 0.9
SQL_FIX_MAX_ROUNDS = 2
# 需要时间序列的意图：表路由时强制要求目标表带时间列
TIME_SERIES_INTENTS = {"trend", "compare"}


async def start_pipeline(*, user_id: str, username: str, conversation_id: str,
                         question: str, confirm: dict | None = None) -> tuple[PipelineContext, asyncio.Task]:
    """启动管线后台任务；SSE 端点消费 ctx.events 队列。"""
    ctx = PipelineContext(user_id=user_id, username=username, conversation_id=conversation_id,
                          question=question, confirm=confirm)
    task = asyncio.create_task(run_pipeline(user_id=user_id, username=username,
                                            conversation_id=conversation_id,
                                            question=question, confirm=confirm, ctx=ctx))
    return ctx, task


# ============================ 主入口 ============================

async def run_pipeline(*, user_id: str, username: str, conversation_id: str,
                       question: str, confirm: dict | None = None,
                       ctx: PipelineContext | None = None) -> PipelineContext:
    if ctx is None:
        ctx = PipelineContext(user_id=user_id, username=username,
                              conversation_id=conversation_id, question=question, confirm=confirm)
    try:
        async with get_session_factory()() as session:
            conv = await session.get(Conversation, _uuid(conversation_id))
            if conv is None:
                raise AppError("会话不存在", code="NOT_FOUND", http_status=404)
            ctx.state = dict(conv.short_term_state or {})
            if conv.llm_config_id:
                ctx.llm_config_id = str(conv.llm_config_id)
            ds_id = conv.datasource_id or ctx.state.get("datasource_id")
            if ds_id:
                ctx.datasource = await session.get(DataSource, _uuid(ds_id))
            if ctx.datasource is None:
                # 会话未绑定数据源：只能在用户有权限的数据源里兜底，防越权
                q = select(DataSource).order_by(DataSource.created_at).limit(1)
                u = await session.get(User, _uuid(user_id))
                if u is not None and u.role != Role.ADMIN:
                    q = q.join(DatasourcePermission,
                               DatasourcePermission.datasource_id == DataSource.id).where(
                                   DatasourcePermission.user_id == u.id,
                                   DatasourcePermission.can_query.is_(True))
                ctx.datasource = (await session.execute(q)).scalar_one_or_none()
            if ctx.datasource is None:
                raise AppError("没有可查询的数据源，请管理员在「用户与权限」页分配", code="NO_DATASOURCE")
            # 脱敏规则预加载
            ctx._mask_rules = [
                (r.column_pattern, r.strategy.value) for r in (await session.execute(
                    select(MaskingRule).where(MaskingRule.datasource_id == ctx.datasource.id,
                                              MaskingRule.enabled.is_(True)))
                ).scalars()]
            ctx.emit("meta", {"conversation_id": conversation_id,
                              "datasource": {"id": str(ctx.datasource.id), "name": ctx.datasource.name,
                                             "db_type": ctx.datasource.db_type.value}})
            # 落库用户消息
            session.add(Message(conversation_id=conv.id, role="user", content=question))
            conv.message_count += 1
            conv.last_active_at = utcnow()
            await session.commit()

        await _pipeline_body(ctx)
        await _finalize(ctx, success=True)
        if ctx.report:
            ctx.emit("report", {"report_id": getattr(ctx, "report_id", ""),
                                "sections": ctx.report["sections"]})
    except _ConfirmRequired:
        await _finalize(ctx, success=False, pending=True)
    except _EmptyResult:
        ctx.answer_text = ("查询结果为空。可能原因：时间范围超出数据覆盖区间、过滤条件过严或口径不匹配。"
                           "可尝试扩大时间范围（如「最近30天」）后重试。")
        ctx.emit("token", {"text": ctx.answer_text})
        await _finalize(ctx, success=True)
    except (AppError, LLMError) as e:
        ctx.error = getattr(e, "message", str(e))
        ctx.emit("error", {"code": getattr(e, "code", "ERROR"), "message": ctx.error,
                           "recoverable": True})
        await _finalize(ctx, success=False)
    except Exception as e:  # noqa: BLE001
        logger.exception("pipeline crashed")
        ctx.error = f"内部错误: {e}"
        ctx.emit("error", {"code": "INTERNAL_ERROR", "message": "处理出错，请稍后重试或换个问法",
                           "recoverable": True})
        await _finalize(ctx, success=False)
    finally:
        try:
            ctx.emit("done", {"duration_ms": ctx.duration_ms, "error": ctx.error})
        except Exception:
            pass
    return ctx


# ============================ 步骤实现 ============================

async def _pipeline_body(ctx: PipelineContext) -> None:
    # ---- 3. 问题改写（多轮指代消解） ----
    st = ctx.step("rewrite")
    ctx.rewritten = await _rewrite(ctx)
    st.finish(ctx.rewritten if ctx.rewritten != ctx.question else "")
    ctx.emit_step(st)

    # ---- 4. 意图路由 ----
    st = ctx.step("route")
    async with get_session_factory()() as session:
        enabled_skills = list((await session.execute(
            select(CustomSkill).where(CustomSkill.status == SkillStatus.ENABLED))
        ).scalars())
    ctx.intent, ctx.intent_conf = await router_agent.route_intent(
        ctx.rewritten, override_config_id=ctx.llm_config_id, custom_skills=enabled_skills)
    st.finish(f"intent={ctx.intent} ({ctx.intent_conf:.2f})")
    ctx.emit_step(st)

    if ctx.intent == "smalltalk":
        await _answer_smalltalk(ctx)
        return

    # 自定义 Skill（PRD 3.3C）：触发词/LLM 命中已启用 Skill
    if ctx.intent.startswith("custom_skill:"):
        name = ctx.intent.split(":", 1)[1]
        skill_row = next((s for s in enabled_skills if s.name.lower() == name.lower()), None)
        await _run_custom_skill(ctx, skill_row, name)
        return

    # 库表答疑（PRD 3.3A）：只查元数据+KG，不走查数链路
    if ctx.intent == "table_qa":
        await _answer_table_qa(ctx)
        return

    # ETL 开发助手（PRD 3.3B）：生成 DDL+加工 SQL，仅输出不执行
    if ctx.intent == "etl_design":
        await _answer_etl(ctx)
        return

    # ---- 5. 记忆检索 ----
    st = ctx.step("memory")
    async with get_session_factory()() as session:
        ctx.profile = await long_term.get_profile(session, _uuid(ctx.user_id))
    similar = await long_term.recall_similar_queries(ctx.user_id, ctx.rewritten)
    st.finish(f"常用指标 {len(ctx.profile.get('favorite_metrics') or [])} 个"
              + (f"；命中相似历史问题 {len(similar)} 条" if similar else ""))
    ctx.emit_step(st)

    # ---- 6. 实体链接 ----
    st = ctx.step("kg_match")
    async with get_session_factory()() as session:
        ctx.linked, ctx.time_range = await entity_linker.link(
            session, ctx.rewritten, ctx.datasource.id,
            confirmed_metric_ids=[ctx.confirm["metric_id"]] if ctx.confirm and ctx.confirm.get("metric_id") else None,
            override_config_id=ctx.llm_config_id)
    # 时间范围兜底：默认最近 7 天
    time_note = ""
    if ctx.time_range is None and ctx.intent in ("metric_query", "trend", "compare",
                                                 "ranking", "share", "drill_down"):
        from datetime import date, timedelta
        from app.agent.nlu.time_parser import TimeRange
        today = date.today()
        ctx.time_range = TimeRange((today - timedelta(days=6)).isoformat(), today.isoformat(),
                                   "最近7天(默认)")
        time_note = "未识别到时间，默认最近7天"
    metrics_payload = [{"id": c.id, "name": c.name, "confidence": round(c.confidence, 2),
                        "why": c.extra.get("why", "")} for c in ctx.linked.metrics]
    dims_payload = [{"id": c.id, "name": c.name} for c in ctx.linked.dimensions]
    if not ctx.linked.metrics:
        st.fail("未匹配到任何指标")
        ctx.emit_step(st)
        raise AppError("知识图谱中未找到相关指标，请换个说法或联系管理员完善图谱",
                       code="NO_METRIC_MATCHED")
    # 低置信 → 发起确认（可中断恢复）
    top = ctx.linked.metrics[0]
    if top.confidence < CONFIRM_THRESHOLD and len(ctx.linked.metrics) > 1 and not ctx.confirm:
        ctx.pending_confirm = metrics_payload[:5]
        st.finish("置信度不足，等待用户确认指标")
        ctx.emit_step(st)
        ctx.emit("kg_match", {"metrics": metrics_payload, "dimensions": dims_payload,
                              "time_range": ctx.time_range.to_dict() if ctx.time_range else None})
        ctx.emit("confirm_required", {"candidates": ctx.pending_confirm})
        raise _ConfirmRequired()
    st.finish(f"指标: {', '.join(c.name for c in ctx.linked.metrics[:5])}"
              + (f"｜维度: {', '.join(c.name for c in ctx.linked.dimensions[:5])}" if ctx.linked.dimensions else ""))
    ctx.emit_step(st)
    ctx.emit("kg_match", {"metrics": metrics_payload, "dimensions": dims_payload,
                          "time_range": ctx.time_range.to_dict() if ctx.time_range else None,
                          "note": time_note})

    # ---- 7. 口径校验 + 权限 ----
    st = ctx.step("validation")
    async with get_session_factory()() as session:
        metric_rows = await kg_service.get_metric_details(
            session, [c.id for c in ctx.linked.metrics])
        dim_rows = await kg_service.get_dimension_details(
            session, [c.id for c in ctx.linked.dimensions])
        # 同词干兄弟指标：命中的指标可能挂在提供不了该维度的表上
        # （「订单量」命中 ads 日汇总表，而问题要按地区拆分）
        metric_rows = await kg_service.expand_metric_siblings(
            session, metric_rows, dim_rows)
        # 按已链接维度收窄指标：别名（如 gmv）会同时命中多张表上的同名指标，
        # 不收窄会因个别指标不兼容该维度而整条链路被约束拦截
        candidate_names = "、".join(m.name for m in metric_rows[:5])
        metric_rows, dropped_rows = await kg_service.filter_metrics_for_dims(
            session, metric_rows, dim_rows)
        if dropped_rows:
            keep = {str(m.id) for m in metric_rows}
            ctx.linked.metrics = [c for c in ctx.linked.metrics if c.id in keep]
        if not metric_rows:
            # 所有候选指标都跑不出这个维度组合：宁可明确报错，也不要挑一条
            # 凑数的指标去笛卡尔积，那会返回一批看似正常、实则错误的数据。
            st.fail("没有指标支持该维度组合")
            ctx.emit_step(st)
            raise AppError(
                f"「{candidate_names or '相关指标'}」无法按该维度拆分：指标所在表既没有该维度列，"
                "也无法通过表关联取得。请改用其它指标，或联系管理员补充表关系",
                code="KG_DIM_UNREACHABLE")
        ctx.metric_rows, ctx.dim_rows = metric_rows, dim_rows
        ctx.violations = await kg_service.check_constraints(session, metric_rows, dim_rows)
        # 物理列名预取：{metric_id: column_name}
        col_ids = {str(m.id): m.column_id for m in metric_rows if m.column_id}
        if col_ids:
            for c in (await session.execute(
                    select(MetaColumn).where(MetaColumn.id.in_(
                        [_uuid(v) for v in col_ids.values()])))).scalars():
                for mid, cid in col_ids.items():
                    if str(c.id) == str(cid):
                        ctx._column_names[mid] = c.column_name
    blocks = [v for v in ctx.violations if v.severity == "block"]
    if blocks:
        st.fail(blocks[0].message)
        ctx.emit_step(st)
        raise AppError("；".join(v.message for v in blocks), code="KG_CONSTRAINT_VIOLATION")
    st.finish("通过" + (f"；提示: {'；'.join(v.message for v in ctx.violations)}" if ctx.violations else ""))
    ctx.emit_step(st)

    # ---- 8. 表路由 ----
    st = ctx.step("route_table")
    async with get_session_factory()() as session:
        # 用 metric_rows 而不是 linked.metrics：口径收窄/兄弟扩张改的是前者，
        # 拿过期的链接结果去路由会漏掉新补进来的候选指标所在的表。
        candidates = await kg_service.resolve_tables(
            session, [str(m.id) for m in ctx.metric_rows],
            [c.id for c in ctx.linked.dimensions],
            need_time=ctx.intent in TIME_SERIES_INTENTS)
        if not candidates:
            raise AppError("指标未映射到物理表，请管理员在图谱中补充指标-表关系",
                           code="NO_TABLE_FOR_METRIC")
        ctx.table = candidates[0]
        table_row = await session.get(MetaTable, _uuid(ctx.table.table_id))
        # 时间列
        time_col_row = (await session.execute(
            select(MetaColumn).where(MetaColumn.table_id == table_row.id,
                                     MetaColumn.semantic_role == "time")
        )).scalars().first()
        ctx.time_col = time_col_row.column_name if time_col_row else ""
        # 维度物理列：{dim_id: column_name}。优先取目标表上的列；
        # 星型/雪花模型里维度落在维表上时，退回该维度在维表上的列名
        # （SQL 会按 JOIN 提示连过去，结果集里仍是这个名字）。
        join_reach = set()
        for h in await kg_service.get_join_hints(session, [ctx.table.table_id]):
            join_reach.add(h.right_table)
        for d in ctx.dim_rows:
            best = None
            for rel in d.related_columns or []:
                if rel.get("table_id") == ctx.table.table_id and rel.get("column_name"):
                    best = rel["column_name"]
                    break
                if rel.get("table_name") in join_reach and rel.get("column_name"):
                    best = best or rel["column_name"]
            if best:
                ctx._dim_column_names[str(d.id)] = best
    st.finish(f"{ctx.table.table_name}（{ctx.table.layer} 层，覆盖指标 {ctx.table.covers_metrics}/{len(ctx.linked.metrics)}）")
    ctx.emit_step(st)

    # ---- 9-10. SQL 生成 + 安全校验（修复循环） ----
    await _generate_and_query(ctx)

    if ctx.query is None:
        return

    # ---- 12. 数据校验 / 计算 / 异常 ----
    await _validate_compute(ctx)

    # ---- 13. 图表 ----
    st = ctx.step("chart")
    spec = _build_chart(ctx)
    ctx.chart_spec = spec.to_dict()
    st.finish(f"{spec.type}")
    ctx.emit_step(st)
    ctx.emit("chart", {"chart_spec": ctx.chart_spec})

    # ---- 14. 联网检索（external 意图） ----
    if ctx.intent == "external":
        st = ctx.step("web")
        result = await tavily.search(f"{ctx.rewritten} 行业数据 2025")
        ctx.web_evidences = [{"title": e.title, "url": e.url, "snippet": e.snippet}
                             for e in result.evidences]
        ctx.web_text = tavily.evidences_to_text(result) if result.ok else (result.error or "")
        st.finish(f"{'命中 ' + str(len(result.evidences)) + ' 条外部参考' if result.ok else '未启用/失败'}")
        ctx.emit_step(st)

    # ---- 15. 流式分析文本 ----
    await _answer_stream(ctx)

    # ---- 16. 报表 ----
    st = ctx.step("report")
    try:
        ctx.report = await report_generate(
            question=ctx.rewritten,
            metrics_info=_metrics_info(ctx),
            data_snapshot=_data_snapshot(ctx),
            anomalies_text=_anomalies_text(ctx),
            compare_text=_compare_text(ctx),
            external_text=ctx.web_text,
            override_config_id=ctx.llm_config_id)
        st.finish("已生成")
    except Exception as e:
        st.fail(str(e)[:120])
    ctx.emit_step(st)


# ============================ 子过程 ============================

class _ConfirmRequired(Exception):
    pass


def _uuid(s):
    """宽容 UUID 转换：接受 str / UUID 两种入参。"""
    import uuid as _u
    return s if isinstance(s, _u.UUID) else _u.UUID(str(s))


async def _rewrite(ctx: PipelineContext) -> str:
    q = ctx.question.strip()
    # 规则：明显是追问（含代词/省略主语）且有历史 → LLM 改写
    followup_pat = re.compile(r"^(那|再|继续|它|他|这个|按|换|加上|去掉|对比一下|呢|拆|细分)")
    needs_rewrite = bool(ctx.state.get("last_question")) and (
        followup_pat.search(q) or len(q) < 12 or "同上" in q or "换成" in q)
    if not needs_rewrite:
        return q
    try:
        from app.db.models.llm import LlmScene
        from app.llm.gateway import gateway
        prompt = (f"历史上下文:\n{short_term.context_prompt(ctx.state)}\n\n"
                  f"用户追问: {q}\n\n把追问改写为独立完整的问题（补全指标名/维度/时间），只输出改写后的问题。")
        resp = await gateway.chat(LlmScene.QUESTION_REWRITE,
                                  [ChatMessage("user", prompt)], temperature=0.0,
                                  override_config_id=ctx.llm_config_id)
        out = resp.text.strip().strip("“”\"'")
        return out or q
    except Exception:
        return q


async def _answer_smalltalk(ctx: PipelineContext) -> None:
    from app.db.models.llm import LlmScene
    from app.llm.gateway import gateway
    intro = ("你是企业数据仓库问答智能体，可以帮助查询指标、做趋势/对比/排行分析并自动生成图表和报表。"
             "当前接入的数据源：" + ctx.datasource.name + "。请友好简短地回复。")
    try:
        async for chunk in gateway.chat_stream(LlmScene.CHAT_ANSWER,
                                               [ChatMessage("system", intro),
                                                ChatMessage("user", ctx.question)],
                                               override_config_id=ctx.llm_config_id):
            if chunk.delta:
                ctx.answer_text += chunk.delta
                ctx.emit("token", {"text": chunk.delta})
    except Exception:
        ctx.answer_text = "你好！我是仓颉（Cangjie），你的 AI 数仓工作台。可以直接问我指标数据，例如「最近7天GMV趋势」或「昨日各渠道销售额排行」。"
        ctx.emit("token", {"text": ctx.answer_text})


async def _run_custom_skill(ctx: PipelineContext, skill_row, name: str) -> None:
    """执行自定义 Skill（PRD 3.3C）：Prompt 型走 LLM 网关，Python 型走沙箱。"""
    from app.agent.skills import table_qa
    from app.services import custom_skills as cs

    st = ctx.step("custom_skill")
    if skill_row is None:
        ctx.answer_text = f"自定义 Skill「{name}」不存在或已停用。"
        ctx.emit("token", {"text": ctx.answer_text})
        st.fail("Skill 不存在/已停用")
        ctx.emit_step(st)
        return

    ok, err = True, ""
    try:
        if skill_row.skill_type == SkillType.PROMPT:
            # 组装占位符上下文（只读元数据快照）
            async with get_session_factory()() as session:
                kg_ctx, _, _ = await table_qa.build_context(
                    session, ctx.datasource.id, ctx.rewritten)
            prompt = cs.render_prompt(
                skill_row.files.get(skill_row.entry, ""),
                question=ctx.rewritten,
                datasource=ctx.datasource.name,
                kg_context=kg_ctx or "（无元数据）")
            from app.db.models.llm import LlmScene
            from app.llm.gateway import gateway
            system = (f"你是企业数仓智能体的扩展技能「{skill_row.name}」：{skill_row.description}\n"
                      "严格遵守：不编造数据与口径；输出简体中文 Markdown。")
            ctx.answer_text = ""
            async for chunk in gateway.chat_stream(
                    LlmScene.CUSTOM_SKILL,
                    [ChatMessage("system", system), ChatMessage("user", prompt)],
                    conversation_id=ctx.conversation_id,
                    override_config_id=ctx.llm_config_id):
                if chunk.delta:
                    ctx.answer_text += chunk.delta
                    ctx.emit("token", {"text": chunk.delta})
        else:
            # Python 型：预取只读快照 → 沙箱执行（沙箱内无任何数据库凭证）
            async with get_session_factory()() as session:
                snapshot = await _skill_snapshot(session, ctx)
            result = await cs.run_python_skill(skill_row.files, skill_row.entry,
                                               ctx.rewritten, snapshot)
            if result.get("ok"):
                ctx.answer_text = str(result.get("result") or "（Skill 无输出）")
                if result.get("log"):
                    ctx.answer_text += f"\n\n<details><summary>执行日志</summary>\n\n```\n{result['log']}\n```\n</details>"
            else:
                ok, err = False, result.get("error", "执行失败")
                ctx.answer_text = f"自定义 Skill「{skill_row.name}」执行失败：{err}"
            ctx.emit("token", {"text": ctx.answer_text})
    except Exception as e:  # noqa: BLE001
        ok, err = False, str(e)[:200]
        logger.exception("custom skill failed")
        ctx.answer_text = f"自定义 Skill「{skill_row.name}」执行异常：{err}"
        ctx.emit("token", {"text": ctx.answer_text})

    # 调用统计 + 审计
    async with get_session_factory()() as session:
        row = await session.get(CustomSkill, skill_row.id)
        if row is not None:
            row.invoke_count += 1
            row.last_invoked_at = utcnow()
        session.add(AuditLog(user_id=_uuid(ctx.user_id), username=ctx.username,
                             action="skill.invoke", resource_type="custom_skill",
                             resource_id=str(skill_row.id),
                             detail={"name": skill_row.name, "version": skill_row.version,
                                     "type": skill_row.skill_type.value, "ok": ok,
                                     "error": err[:300], "duration_ms": ctx.duration_ms}))
        await session.commit()
    if ok:
        st.finish(f"{skill_row.name} v{skill_row.version}")
    else:
        st.fail(err[:120])
    ctx.emit_step(st)


async def _skill_snapshot(session, ctx: PipelineContext) -> dict:
    """Python Skill 的只读上下文快照：数据源 + 表清单 + 指标清单（不含任何凭证）。"""
    tables = (await session.execute(
        select(MetaTable).where(MetaTable.datasource_id == ctx.datasource.id)
        .order_by(MetaTable.layer, MetaTable.table_name).limit(200))).scalars().all()
    from app.db.models import KgMetric, MetricStatus
    metrics = (await session.execute(
        select(KgMetric).where(KgMetric.status == MetricStatus.ACTIVE).limit(200))
    ).scalars().all()
    return {
        "question": ctx.rewritten,
        "datasource": {"name": ctx.datasource.name, "db_type": ctx.datasource.db_type.value},
        "tables": [{"name": t.table_name, "layer": t.layer.value,
                    "comment": t.comment, "partition_cols": t.partition_cols or []}
                   for t in tables],
        "metrics": [{"name": m.name, "def": m.business_def or "", "unit": m.unit or ""}
                    for m in metrics],
    }


async def _answer_table_qa(ctx: PipelineContext) -> None:
    """库表答疑（PRD 3.3A）：上下文只来自元数据+KG，流式输出。"""
    from app.agent.skills import table_qa
    st = ctx.step("table_qa")
    ctx.answer_text = ""
    matched: list[str] = []
    cards: list[dict] = []
    async with get_session_factory()() as session:
        async for delta, matched, cards in table_qa.table_qa_answer(
                session=session, question=ctx.rewritten,
                datasource_id=ctx.datasource.id,
                override_config_id=ctx.llm_config_id):
            ctx.answer_text += delta
            ctx.emit("token", {"text": delta})
    if cards:
        ctx.metric_cards = cards
        ctx.emit("metric_cards", {"metrics": cards})
    detail = []
    if matched:
        detail.append(f"命中表 {len(matched)} 张")
    if cards:
        detail.append(f"命中指标 {len(cards)} 个")
    st.finish("，".join(detail) if detail else "选表建议（未指名表）")
    ctx.emit_step(st)


async def _answer_etl(ctx: PipelineContext) -> None:
    """ETL 开发助手（PRD 3.3B）：子 Agent 规划→起草→校验→修复，仅输出不执行。"""
    from app.agent.skills import etl
    async with get_session_factory()() as session:
        design = await etl.etl_design(session=session, question=ctx.rewritten,
                                      datasource=ctx.datasource,
                                      override_config_id=ctx.llm_config_id,
                                      ctx=ctx)  # 子步骤 SSE 由 skill 内部发射
    ctx.etl_design = design.to_dict()
    ctx.emit("etl", ctx.etl_design)
    ctx.answer_text = design.to_markdown()
    ctx.emit("token", {"text": ctx.answer_text})


async def _generate_and_query(ctx: PipelineContext) -> None:
    dialect = ctx.datasource.db_type.value
    # _generate_and_query 只在表路由之后调用，here ctx.table 必然已确定
    assert ctx.table is not None
    async with get_session_factory()() as session:
        # metric_context 内部会带上目标表的 JOIN 提示（跨表维度靠它落地物理列）
        kg_ctx = await kg_service.metric_context(
            session, ctx.metric_rows,
            await session.get(MetaTable, _uuid(ctx.table.table_id)), ctx.dim_rows)

    # 缓存快查
    time_expr = _time_expr(ctx)
    st_sql = ctx.step("sql")
    st_guard = ctx.step("guard")

    sql = await sql_gen.sql_generate(
        question=ctx.rewritten, kg_context=kg_ctx, dialect=dialect,
        time_expr=time_expr,
        extra_rules=_extra_rules(ctx), override_config_id=ctx.llm_config_id)
    st_sql.finish()
    ctx.emit_step(st_sql)
    ctx.emit("sql", {"sql": sql, "tables": [ctx.table.table_name], "dialect": dialect,
                     "guarded": False})

    # 允许执行的表 = 元数据已登记的表（白名单）∧ 用户权限由数据源级控制
    async with get_session_factory()() as session:
        allowed_tables = set((await session.execute(
            select(MetaTable.table_name).where(MetaTable.datasource_id == ctx.datasource.id)
        )).scalars())

    last_error = ""
    for round_i in range(SQL_FIX_MAX_ROUNDS + 1):
        g = guard_sql(sql, dialect, allowed_tables, settings.sql_max_rows,
                      current_db=ctx.datasource.database_name if ctx.datasource else "")
        if g.ok:
            st_guard.finish("通过（含 LIMIT 保护）")
            ctx.emit_step(st_guard)
            ctx.sql, ctx.guarded_sql = sql, g.sql
            ctx.emit("sql", {"sql": g.sql, "tables": g.tables, "dialect": dialect, "guarded": True})
            break
        last_error = "；".join(g.violations)
        st_guard.fail(last_error[:200])
        ctx.emit_step(st_guard)
        if round_i == SQL_FIX_MAX_ROUNDS:
            raise AppError(f"SQL 安全校验未通过: {last_error}", code="SQL_GUARD_FAILED")
        sql = await sql_gen.sql_fix(question=ctx.rewritten, kg_context=kg_ctx,
                                    dialect=dialect, bad_sql=sql, errors=last_error,
                                    override_config_id=ctx.llm_config_id)
    else:
        raise AppError("SQL 安全校验未通过", code="SQL_GUARD_FAILED")

    # ---- 11. 执行（含缓存） ----
    from app.services import cache_service
    st_query = ctx.step("query")
    key = _cache_key(ctx)
    async with get_session_factory()() as session:
        cached = await cache_service.get_cache(session, key)
        if cached:
            await session.commit()   # 命中计数落库（否则退出即回滚）
    connector = get_connector(ctx.datasource)
    if cached:
        ctx.cache_hit = True
        from app.connectors.base import QueryResult
        ctx.query = QueryResult(**cached["result"])
        if cached.get("chart_spec"):
            ctx.chart_spec = cached["chart_spec"]
        if cached.get("answer_text"):
            ctx.answer_text = cached["answer_text"]
        st_query.finish(f"缓存命中（毫秒级返回），{ctx.query.row_count} 行")
        ctx.emit_step(st_query)
        return

    try:
        qr = await connector.execute(ctx.guarded_sql)
    except Exception as e:
        err = f"{e.__class__.__name__}: {e}"
        st_query.fail(err[:200])
        ctx.emit_step(st_query)
        # 执行错误回喂修复一轮
        if "syntax" in err.lower() or "error" in err.lower():
            try:
                fixed = await sql_gen.sql_fix(question=ctx.rewritten, kg_context=kg_ctx,
                                              dialect=dialect, bad_sql=ctx.guarded_sql, errors=err,
                                              override_config_id=ctx.llm_config_id)
                g2 = guard_sql(fixed, dialect, allowed_tables, settings.sql_max_rows,
                        current_db=ctx.datasource.database_name if ctx.datasource else "")
                if g2.ok:
                    qr = await connector.execute(g2.sql)
                    ctx.guarded_sql = g2.sql
                    ctx.emit("sql", {"sql": g2.sql, "tables": g2.tables, "dialect": dialect,
                                     "guarded": True})
                    st_query.status = "finished"
                    st_query.finish(f"自动纠错后成功，{qr.row_count} 行")
                    ctx.emit_step(st_query)
                    ctx.query = qr
                    return
            except Exception as e2:
                raise AppError(f"数仓查询失败: {str(e2)[:200]}", code="QUERY_FAILED") from e2
        raise AppError(f"数仓查询失败: {err[:200]}", code="QUERY_FAILED")
    ctx.query = qr
    st_query.finish(f"{qr.row_count} 行 / {qr.duration_ms}ms")
    ctx.emit_step(st_query)


def _time_expr(ctx: PipelineContext) -> str:
    if not ctx.time_range:
        return "（问题未含时间，请按数据最近日期取最新一天）"
    tr = ctx.time_range
    return (f"{ctx.time_col or '时间字段'} 在 {tr.start} 至 {tr.end}（含两端；"
            f"datetime 类型结束日取 23:59:59；可直接写 BETWEEN '{tr.start}' AND '{tr.end}'）")


def _extra_rules(ctx: PipelineContext) -> str:
    rules: list[str] = []
    intent = ctx.intent
    if intent == "ranking":
        m = re.search(r"top\s*(\d+)|前\s*(\d+)", ctx.rewritten, re.I)
        n = next((int(x) for x in m.groups() if x), 10) if m else 10
        rules.append(f"排行分析：按主指标降序排序，只输出前 {n} 行（LIMIT {n}）")
    if intent == "trend":
        rules.append("趋势分析：按时间字段分组（日粒度），返回时间序列")
    if intent == "share":
        rules.append("占比分析：按维度分组聚合")
    dims = [c.name for c in ctx.linked.dimensions]
    if dims:
        rules.append(f"维度拆解：GROUP BY {'、'.join(dims)} 对应的物理列")
    if ctx.time_range:
        rules.append(f"统计周期：{ctx.time_range.label}")
    return "；".join(rules)


async def _validate_compute(ctx: PipelineContext) -> None:
    st_check = ctx.step("check")
    metric_cols = _metric_columns(ctx)
    value_ranges = _value_ranges(ctx, ctx.query.rows)
    cleaned, validation = validate_result(
        ctx.query.columns, ctx.query.rows, metric_cols=list(metric_cols),
        value_ranges=value_ranges,
        expected_dims=[_dim_column(ctx, c) for c in ctx.linked.dimensions if _dim_column(ctx, c)])
    # 缓存命中的行在写入缓存时已脱敏，不再二次脱敏（mask_hash 二次哈希会改变值）
    ctx.cleaned_rows = cleaned if ctx.cache_hit else _apply_masking(ctx, cleaned)
    ctx.validation = validation.to_dict()
    if validation.empty:
        st_check.finish("结果为空")
        ctx.emit_step(st_check)
        raise _EmptyResult()
    st_check.finish("通过" + (f"；{'；'.join(validation.warnings)}" if validation.warnings else ""))
    ctx.emit_step(st_check)

    # 对比期计算（环比/同比）
    st = ctx.step("compute")
    need_yoy = any(w in ctx.rewritten for w in YOY_WORDS)
    need_mom = any(w in ctx.rewritten for w in MOM_WORDS)
    if (need_yoy or need_mom) and ctx.time_range:
        base = ctx.time_range.yoy_period() if need_yoy else ctx.time_range.prev_period()
        label = "去年同期" if need_yoy else "上一周期"
        try:
            base_sql = ctx.guarded_sql.replace(ctx.time_range.start, base.start).replace(
                ctx.time_range.end, base.end)
            if base_sql == ctx.guarded_sql:
                # SQL 里找不到可替换的日期字面量（如 date_sub 表达式）——
                # 宁可不对比，也不拿同一份数据算假变化率
                ctx.compare = None
            else:
                from app.connectors.base import QueryResult as QR
                base_qr = await get_connector(ctx.datasource).execute(base_sql)
                ctx.compare = {"rows": _apply_masking(ctx, base_qr.rows), "label": label}
        except Exception:
            ctx.compare = None
        # 对比期覆盖度检查：明细表保留期常短于汇总表，上一周期的窗口可能
        # 只落得下一天的数据。拿 1 天的合计去比 30 天的合计会算出 +2500%
        # 这种假变化率，数字看着像真的，结论全错 —— 宁可不给变化率。
        cur_span = _time_span(ctx.cleaned_rows, ctx.time_col)
        prev_span = _time_span((ctx.compare or {}).get("rows") or [], ctx.time_col)
        if ctx.compare and cur_span and prev_span < cur_span * 0.8:
            ctx.compare["incomplete"] = (prev_span, cur_span)
        # 对比计算按「结果列名」而非 KG 物理列名取值：SQL 起了别名时
        # 物理列名在结果里不存在，会一路算出「无前期数据」。
        result_cols = _result_metric_cols(ctx, ctx.cleaned_rows) or list(metric_cols)
        comp = compute.compare_rows(ctx.cleaned_rows, (ctx.compare or {}).get("rows") or [],
                                    result_cols)
        if (ctx.compare or {}).get("incomplete"):
            for c in comp:
                c.pct = None
                c.delta = None
            note = (f"{label}仅覆盖 {prev_span}/{cur_span} 个时间点，"
                    "变化率不具可比性，已略去")
            ctx.validation["warnings"] = (ctx.validation.get("warnings") or []) + [note]
            st.finish(f"{label}对比: " + "；".join(
                f"{c.name} 本期 {c.current}，{note}" for c in comp[:2]))
        else:
            st.finish(f"{label}对比: " + "；".join(
                f"{c.name} {c.current}（{('+' if (c.pct or 0) >= 0 else '') + str(c.pct) + '%' if c.pct is not None else '无前期数据'}）"
                for c in comp[:4]))
    else:
        st.finish("无需对比计算")
    ctx.emit_step(st)

    # 异常检测（趋势序列）
    st = ctx.step("anomaly")
    if ctx.time_col in (ctx.query.columns or []) and ctx.cleaned_rows:
        # 同样要用结果列名：SQL 起别名时物理列名不在结果里，
        # 取出来全是空值，序列塌成一条零线，异常检测就成了摆设。
        result_cols = _result_metric_cols(ctx, ctx.cleaned_rows)
        first_metric = result_cols[0] if result_cols else None
        if first_metric:
            # 结果里带维度（跨表维度趋势）时，同一日期会出现多行。
            # 必须按日期先聚合再检测，否则序列里是渠道交错的值，
            # 环比与离群点全是假的。
            xs, vals = _daily_series(ctx, first_metric)
            rep = detect_anomalies(xs, vals)
            rep.missing_dates = find_missing_dates(xs)
            ctx.anomalies = rep.to_dict()
            n = len(rep.points)
            st.finish((f"检出 {n} 个异常点" + (f"、缺失 {len(rep.missing_dates)} 天" if rep.missing_dates else ""))
                      if (n or rep.missing_dates) else "无异常")
            ctx.emit_step(st)
            return
    st.finish("跳过（非时间序列）")
    ctx.emit_step(st)


def _daily_series(ctx: PipelineContext, metric_col: str) -> tuple[list[str], list]:
    """按时间列汇总出单指标序列：同一天多行（维度拆解）先相加再排序。"""
    order: list[str] = []
    agg: dict[str, float] = {}
    for r in ctx.cleaned_rows:
        x = str(r.get(ctx.time_col))
        if x not in agg:
            order.append(x)
            agg[x] = 0.0
        v = _num(r.get(metric_col))
        if v is not None:
            agg[x] += v
    order.sort()
    return order, [agg[x] for x in order]


def _build_chart(ctx: PipelineContext) -> object:
    metric_cols = list(_metric_columns(ctx))
    dim_cols = [c for c in (_dim_column(ctx, c) for c in ctx.linked.dimensions) if c]
    unit = next((m.unit for m in ctx.metric_rows if m.unit), "")
    title = ctx.rewritten[:60]
    anomalies = [
        {"x": a.get("x"), "value": a.get("value"), "direction": a.get("direction"),
         "note": a.get("note"), "severity": a.get("severity")}
        for a in (ctx.anomalies.get("points") or [])
    ][:20]
    return build_chart_spec(
        title=title, intent=ctx.intent, columns=ctx.query.columns, rows=ctx.cleaned_rows,
        metric_cols=metric_cols, dim_cols=dim_cols, time_col=ctx.time_col,
        anomalies=anomalies, unit=unit, compare=ctx.compare)


async def _answer_stream(ctx: PipelineContext) -> None:
    from app.db.models.llm import LlmScene
    from app.llm.gateway import gateway
    st = ctx.step("answer")
    if ctx.cache_hit and ctx.answer_text:
        ctx.emit("token", {"text": ctx.answer_text})
        st.finish("缓存命中")
        ctx.emit_step(st)
        return
    prompt = _answer_prompt(ctx)
    ctx.answer_text = ""
    try:
        async for chunk in gateway.chat_stream(
                LlmScene.CHAT_ANSWER,
                [ChatMessage("system", _ANSWER_SYSTEM), ChatMessage("user", prompt)],
                conversation_id=ctx.conversation_id,
                override_config_id=ctx.llm_config_id):
            if chunk.delta:
                ctx.answer_text += chunk.delta
                ctx.emit("token", {"text": chunk.delta})
        st.finish(f"{len(ctx.answer_text)} 字")
    except Exception as e:
        # LLM 失败但已有数据 → 降级返回原始表格摘要（PRD 4.3）
        ctx.answer_text = _plain_summary(ctx)
        ctx.emit("token", {"text": ctx.answer_text})
        st.fail(f"LLM 不可用，降级为摘要: {str(e)[:80]}")
    ctx.emit_step(st)


_ANSWER_SYSTEM = """你是企业数据分析专家，基于给出的真实查询结果回答用户问题。要求：
1. 结论先行：第一句直接给答案（带具体数字和单位）。
2. 简明扼要：3-6 句话；必要时用 1 个简短列表；不要重复表格全部数据。
3. 若有对比/异常信息，点出关键变化与可能原因；禁止编造数据中没有的数字。
4. 使用简体中文。"""


def _answer_prompt(ctx: PipelineContext) -> str:
    parts = [f"<问题>{ctx.rewritten}</问题>",
             f"<指标口径>{_metrics_info(ctx)}</指标口径>",
             f"<查询结果>{_data_snapshot(ctx)}</查询结果>"]
    if ctx.compare:
        parts.append(f"<对比信息>{_compare_text(ctx)}</对比信息>")
    if ctx.anomalies:
        parts.append(f"<异常检测>{_anomalies_text(ctx)}</异常检测>")
    if ctx.validation.get("warnings"):
        parts.append(f"<数据提示>{'；'.join(ctx.validation['warnings'])}</数据提示>")
    parts.append("请给出分析回答：")
    return "\n\n".join(parts)


def _metrics_info(ctx: PipelineContext) -> str:
    lines = []
    cols = _metric_columns(ctx)
    for m in ctx.metric_rows:
        col = next((name for name, mm in cols.items() if mm.id == m.id), None) or m.name
        lines.append(f"- {m.name}（{col}）: {m.business_def or '口径见图谱'}"
                     + (f"，聚合 {m.agg_function}" if m.agg_function else "")
                     + (f"，单位 {m.unit}" if m.unit else ""))
    return "\n".join(lines)


def _metric_columns(ctx: PipelineContext) -> dict:
    """{物理列名: metric_row}（基于预取的 _column_names，带缓存）。"""
    cached = getattr(ctx, "_metric_col_cache", None)
    if cached is not None:
        return cached
    out: dict = {}
    for m in ctx.metric_rows:
        name = ctx._column_names.get(str(m.id))
        if name:
            out[name] = m
    ctx._metric_col_cache = out  # type: ignore[attr-defined]
    return out


def _dim_column(ctx: PipelineContext, cand) -> str | None:
    return ctx._dim_column_names.get(str(cand.id))


def _result_metric_cols(ctx: PipelineContext, rows: list[dict]) -> list[str]:
    """结果集里真正承载指标的列名。

    LLM 常给聚合列起别名（`SUM(f.gmv) AS m`），KG 里的物理列名对不上结果列，
    按物理列名取值会全部拿到 None —— 同比/环比数值、值域校验、回答里的统计
    摘要都会静默失效（图表那边另有 _resolve_metric_cols 兜底，这里复用同一判据）。
    """
    metric_cols = list(_metric_columns(ctx))
    if not metric_cols or not rows:
        return []
    exclude = {c for c in (_dim_column(ctx, d) for d in ctx.dim_rows) if c}
    if ctx.time_col:
        exclude.add(ctx.time_col)
    return _resolve_metric_cols(ctx.query.columns, rows, metric_cols, exclude)


def _time_span(rows: list[dict], time_col: str) -> int:
    """结果里出现的不同时间点个数（用来判断对比期窗口是否被数据保留期截断）。"""
    if not time_col:
        return 0
    return len({str(r.get(time_col)) for r in rows if r.get(time_col) is not None})


def _value_ranges(ctx: PipelineContext, rows: list[dict]) -> dict[str, list]:
    """{结果列名: 值域}。仅在单指标场景下才按位置回退到别名列 ——
    多指标时列名对不上就没法判断哪个值域属于哪列，猜错会报出假告警。"""
    metric_cols = _metric_columns(ctx)
    out = {c: m.value_range for c, m in metric_cols.items() if m.value_range}
    if len(metric_cols) == 1 and len(out) == 1 and not (set(out) & set(ctx.query.columns or [])):
        result_cols = _result_metric_cols(ctx, rows)
        if len(result_cols) == 1:
            out[result_cols[0]] = next(iter(out.values()))
    return out


class _EmptyResult(Exception):
    pass


def _data_snapshot(ctx: PipelineContext) -> str:
    rows = ctx.cleaned_rows[:20]
    cols = ctx.query.columns[:12]
    head = " | ".join(cols)
    lines = [head, "-" * min(80, len(head) * 2)]
    for r in rows:
        lines.append(" | ".join(str(r.get(c, ""))[:24] for c in cols))
    metric_cols = _result_metric_cols(ctx, ctx.cleaned_rows) or list(_metric_columns(ctx))
    stats = {mc: compute.series_stats([r.get(mc) for r in ctx.cleaned_rows]) for mc in metric_cols[:5]}
    stat_lines = [f"{k}: 合计={v.get('sum')} 均值={v.get('mean')} 最大={v.get('max')} 最小={v.get('min')}"
                  for k, v in stats.items() if v.get("count")]
    total = f"\n共 {len(ctx.cleaned_rows)} 行（仅展示前 {len(rows)} 行）" if len(ctx.cleaned_rows) > len(rows) else ""
    return "\n".join(lines) + total + ("\n统计：" + "；".join(stat_lines) if stat_lines else "")


def _compare_text(ctx: PipelineContext) -> str:
    if not ctx.compare:
        return ""
    comp = compute.compare_rows(ctx.cleaned_rows, ctx.compare.get("rows") or [],
                                _result_metric_cols(ctx, ctx.cleaned_rows)
                                or list(_metric_columns(ctx)))
    return "；".join(f"{c.name}: {c.current} vs {c.previous}"
                     + (f"（{('+' if (c.pct or 0) >= 0 else '')}{c.pct}%）" if c.pct is not None else "")
                     for c in comp)


def _anomalies_text(ctx: PipelineContext) -> str:
    pts = ctx.anomalies.get("points") or []
    if not pts and not ctx.anomalies.get("missing_dates"):
        return ""
    lines = [f"在 {p['x']} 出现{('上涨' if p['direction'] == 'up' else '下跌')}异常: {p['value']}（{p['note']}）"
             for p in pts[:8]]
    if ctx.anomalies.get("missing_dates"):
        lines.append(f"日期缺失: {', '.join(ctx.anomalies['missing_dates'][:10])}")
    return "\n".join(lines)


def _plain_summary(ctx: PipelineContext) -> str:
    stats = []
    for mc in list(_metric_columns(ctx))[:3]:
        st = compute.series_stats([r.get(mc) for r in ctx.cleaned_rows])
        if st.get("count"):
            stats.append(f"{mc} 合计 {st['sum']}")
    return f"（LLM 服务暂不可用，原始数据摘要）共 {len(ctx.cleaned_rows)} 行；" + "；".join(stats)


def _cache_key(ctx: PipelineContext) -> str:
    """查询缓存键：数据源 + SQL + 脱敏规则签名。

    缓存里存的是脱敏后的行数据；脱敏规则变更后签名变化，旧缓存自然不再命中，
    避免按过期口径把旧数据发给用户。
    """
    from app.services import cache_service
    rules = getattr(ctx, "_mask_rules", None) or []
    sig = ";".join(f"{p}:{s}" for p, s in sorted(rules))
    return cache_service.cache_key(str(ctx.datasource.id), ctx.guarded_sql + "#mask:" + sig)


def _apply_masking(ctx: PipelineContext, rows: list[dict]) -> list[dict]:
    """按脱敏规则（列名模式）处理结果行。"""
    rules = getattr(ctx, "_mask_rules", None)
    if not rules:
        return rows
    import fnmatch
    import hashlib

    def strategy_for(col: str) -> str | None:
        for pattern, strategy in rules:
            if fnmatch.fnmatch(col.lower(), pattern.lower()):
                return strategy
        return None

    out = []
    for r in rows:
        r = dict(r)
        for col in list(r.keys()):
            strategy = strategy_for(col)
            if strategy and r[col] is not None:
                v = str(r[col])
                if strategy == "mask_partial":
                    r[col] = (v[:3] + "***" + v[-2:]) if len(v) > 6 else "***"
                elif strategy == "mask_hash":
                    r[col] = hashlib.sha256(v.encode()).hexdigest()[:12]
                else:
                    r[col] = None
        out.append(r)
    return out


# ============================ 收尾 ============================

async def _finalize(ctx: PipelineContext, *, success: bool, pending: bool = False) -> None:
    from app.services import cache_service
    async with get_session_factory()() as session:
        payload: dict = {
            "steps": [s.to_dict() for s in ctx.steps],
            "intent": ctx.intent,
            "rewritten": ctx.rewritten,
            "matched_metrics": [{"id": c.id, "name": c.name} for c in (ctx.linked.metrics if ctx.linked else [])],
            "dimensions": [{"id": c.id, "name": c.name} for c in (ctx.linked.dimensions if ctx.linked else [])],
            "time_range": ctx.time_range.to_dict() if ctx.time_range else None,
            "sql": ctx.guarded_sql,
            "chart_spec": ctx.chart_spec,
            "anomalies": ctx.anomalies,
            "validation": ctx.validation,
            "web_evidences": ctx.web_evidences,
            "cache_hit": ctx.cache_hit,
            "error": ctx.error,
            "etl": ctx.etl_design,
            "metric_cards": getattr(ctx, "metric_cards", None),
        }
        if pending:
            payload["pending_confirm"] = ctx.pending_confirm
        if ctx.report:
            report_row = Report(user_id=_uuid(ctx.user_id),
                                conversation_id=_uuid(ctx.conversation_id),
                                title=ctx.report["title"],
                                content_md=ctx.report["content_md"],
                                sections=ctx.report["sections"])
            session.add(report_row)
            await session.flush()
            payload["report"] = {"id": str(report_row.id), "title": ctx.report["title"]}
            ctx.report_id = str(report_row.id)  # type: ignore[attr-defined]

        session.add(Message(conversation_id=_uuid(ctx.conversation_id), role="assistant",
                            content=ctx.answer_text or ctx.error or "", payload=payload))
        conv = await session.get(Conversation, _uuid(ctx.conversation_id))
        if conv:
            conv.message_count += 1
            conv.last_active_at = utcnow()
            if ctx.linked and ctx.linked.metrics:
                conv.short_term_state = short_term.merge_state(
                    ctx.state,
                    metric_names=[c.name for c in ctx.linked.metrics],
                    dim_names=[c.name for c in ctx.linked.dimensions],
                    time_range=ctx.time_range.to_dict() if ctx.time_range else None,
                    last_sql=ctx.guarded_sql or None, question=ctx.question,
                    datasource_id=str(ctx.datasource.id) if ctx.datasource else None)

        # 查询记录（成功/空结果/被拦截）
        if ctx.intent not in ("smalltalk", "") and ctx.linked is not None:
            status = QueryStatus.SUCCESS
            if pending:
                status = QueryStatus.BLOCKED
            elif not success:
                status = QueryStatus.ERROR
            elif ctx.validation.get("empty"):
                status = QueryStatus.EMPTY
            qr = QueryRecord(
                user_id=_uuid(ctx.user_id), conversation_id=_uuid(ctx.conversation_id),
                datasource_id=ctx.datasource.id if ctx.datasource else None,
                question=ctx.question, rewritten_question=ctx.rewritten,
                matched_metric_ids=[c.id for c in ctx.linked.metrics],
                used_dimensions=[c.name for c in ctx.linked.dimensions],
                time_range=ctx.time_range.to_dict() if ctx.time_range else {},
                sql_text=ctx.guarded_sql,
                # sql_hash 供夜间反思按「同类查询」聚合沉淀报表模板
                sql_hash=(cache_service.cache_key(str(ctx.datasource.id), ctx.guarded_sql)
                          if ctx.datasource and ctx.guarded_sql else ""),
                status=status,
                row_count=len(ctx.cleaned_rows), duration_ms=ctx.duration_ms,
                cache_hit=ctx.cache_hit, error_brief=ctx.error[:500],
                chart_type=(ctx.chart_spec or {}).get("type", ""))
            session.add(qr)
            await session.flush()
            ctx.query_record_id = str(qr.id)

        # 缓存写入 + 记忆回写
        if success and ctx.query is not None and not ctx.cache_hit:
            # 存脱敏后的行数据（cleaned_rows）：命中链路直接复用，不再经过执行器
            key = _cache_key(ctx)
            await cache_service.set_cache(
                session, key,
                {"columns": ctx.query.columns, "rows": ctx.cleaned_rows[:500],
                 "row_count": ctx.query.row_count, "duration_ms": ctx.query.duration_ms},
                ctx.chart_spec, ctx.answer_text)
        await session.commit()

    if success and ctx.linked and ctx.linked.metrics:
        metric_names = {c.id: c.name for c in ctx.linked.metrics}
        async with get_session_factory()() as session:
            await long_term.update_profile_stats(
                session, _uuid(ctx.user_id), [c.id for c in ctx.linked.metrics], metric_names,
                [c.name for c in ctx.linked.dimensions])
            await session.commit()
        await long_term.remember_query(
            ctx.user_id, ctx.rewritten or ctx.question,
            [c.id for c in ctx.linked.metrics],
            ctx.guarded_sql[:200])
