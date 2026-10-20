"""管线收尾：缓存键、列脱敏、结果落库（查询记录/报表/缓存/审计）（自 orchestrator 拆分）。"""
from __future__ import annotations

import logging


from app.agent.memory import long_term, short_term
from app.agent.pipeline.context import PipelineContext, _uuid
from app.db.models.base import utcnow
from app.db.models import (Conversation, Message,
                           QueryRecord, QueryStatus, Report)
from app.db.session import get_session_factory

logger = logging.getLogger(__name__)

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
