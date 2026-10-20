"""结果分析助手：图表构建、指标/维度列推断、数据快照与异常文本（自 orchestrator 拆分）。"""
from __future__ import annotations



from app.agent.pipeline.context import PipelineContext
from app.agent.skills import compute
from app.agent.skills.chart import _resolve_metric_cols, build_chart_spec
from app.agent.skills.compute import _num

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
