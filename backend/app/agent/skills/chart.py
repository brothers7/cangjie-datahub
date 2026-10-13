"""图表渲染 Skill：场景匹配 + ChartSpec 生成（前端 ECharts 统一契约）。"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.skills.compute import _num, share_breakdown, series_stats


@dataclass
class ChartSpec:
    type: str = "table"            # stat_card / line / bar / hbar / pie / table
    title: str = ""
    unit: str = ""
    x: list = field(default_factory=list)
    series: list = field(default_factory=list)   # [{name, data}]
    cards: list = field(default_factory=list)    # stat_card: [{label, value, delta, pct, trend, sub}]
    pies: list = field(default_factory=list)     # pie: [{name, value, share}]
    columns: list = field(default_factory=list)  # table
    rows: list = field(default_factory=list)     # table
    anomalies: list = field(default_factory=list)  # [{x, value, direction, note, severity}]
    compare_label: str = ""        # 对比图例说明

    def to_dict(self) -> dict:
        return {
            "type": self.type, "title": self.title, "unit": self.unit,
            "x": self.x, "series": self.series, "cards": self.cards, "pies": self.pies,
            "columns": self.columns, "rows": self.rows, "anomalies": self.anomalies,
            "compare_label": self.compare_label,
        }


def _is_date_like(v) -> bool:
    s = str(v)
    return len(s) >= 8 and (s[:4].isdigit() and "-" in s)


def _numeric_ratio(rows: list[dict], col: str) -> float:
    if not rows:
        return 0.0
    ok = sum(1 for r in rows if isinstance(r.get(col), (int, float))
             and not isinstance(r.get(col), bool))
    return ok / len(rows)


def _resolve_metric_cols(columns: list[str], rows: list[dict], metric_cols: list[str],
                         exclude: set[str]) -> list[str]:
    """指标物理列定位。LLM 常给指标起别名（SUM(gmv) AS 销售额），
    物理列名对不上时，退回「既不是维度也不是时间的那几列」，
    否则排行/占比图会拿着一列全 None 渲染出空白图。"""
    present = [c for c in metric_cols if c in columns]
    if present:
        return present
    candidates = [c for c in columns if c not in exclude]
    # 优先数值列，避免把「订单号」这类文本列当成指标
    numeric = [c for c in candidates if _numeric_ratio(rows, c) >= 0.5]
    return numeric or candidates


def _index_align(pairs: list[tuple[str, float]], x_len: int) -> list:
    """按「周期内位置」对齐取值，长度不足补 None。

    对比期的日期标签与本期是错开的（去年同期差一年，环比差一个周期长），
    按日期对齐会全部落空、画出一条空线。按行内位置叠加才是同比/环比的本意。
    周期长度不一致（被数据保留期截断）时只补齐、不拉伸，
    避免把半段的数值铺满整条线。
    """
    ordered = sorted(pairs, key=lambda kv: kv[0])
    vals = [v for _, v in ordered][:x_len]
    return vals + [None] * (x_len - len(vals))


def _pivot_by_dim(rows: list[dict], time_col: str, dim_col: str,
                  metric_col: str, x_len: int | None = None) -> tuple[list[str], dict[str, list]]:
    """把「时间 × 维度 × 指标」明细行透视为折线图所需的 {维度值: [按时间的值]}。

    跨表维度（如「各渠道 GMV 趋势」）的 SQL 会同时带时间列和维度列。
    直接拿指标列当纵轴会把不同渠道的点串成一条乱线 —— 看起来是趋势，
    实际是几个渠道的高频交错，数值量级完全失真。

    x_len 给出时按周期内位置对齐（对比期用），否则按日期对齐。
    """
    xs: list[str] = []
    seen: set[str] = set()
    pts: dict[str, list[tuple[str, float]]] = {}
    for r in rows:
        x = str(r.get(time_col))
        if x not in seen:
            seen.add(x)
            xs.append(x)
        key = str(r.get(dim_col, "未知"))
        v = _num(r.get(metric_col))
        if v is not None:
            pts.setdefault(key, []).append((x, v))
    if x_len is not None:
        return xs, {k: _index_align(v, x_len) for k, v in pts.items()}
    return xs, {k: [dict(v).get(x) for x in xs] for k, v in pts.items()}


def _align_series(rows: list[dict], x_len: int, time_col: str,
                  metric_col: str) -> list:
    """单序列的对比期对齐（按周期内位置），缺失位留 None。"""
    pairs = [(str(r.get(time_col)), v) for r in rows
             if (v := _num(r.get(metric_col))) is not None]
    # 同一天可能有多行（不同维度），先按日期汇总再对齐
    agg: dict[str, float] = {}
    for x, v in pairs:
        agg[x] = agg.get(x, 0.0) + v
    return _index_align(list(agg.items()), x_len)


def build_chart_spec(*, title: str, intent: str, columns: list[str], rows: list[dict],
                     metric_cols: list[str], dim_cols: list[str], time_col: str | None,
                     anomalies: list[dict] | None = None, unit: str = "",
                     compare: dict | None = None) -> ChartSpec:
    """自动匹配场景生成图表（PRD 3.6）：
    单指标时点→数值卡片；指标×时间→折线；占比→饼图；TopN/对比→柱状；其余→明细表。
    """
    anomalies = anomalies or []
    spec = ChartSpec(title=title, unit=unit, anomalies=anomalies)

    has_time = bool(time_col and rows and _is_date_like(rows[0].get(time_col, "")))
    # 时间 + 维度同时存在（跨表维度趋势）时要按维度分线，不能只留时间列
    breakdown_col = next((d for d in dim_cols if d in columns), None) if has_time else None
    dim_col = next((d for d in dim_cols if d in columns), None)
    # LLM 常给列起中文别名（channel_name AS 渠道），物理列名匹配不上时
    # 退回「第一个非指标列」，否则排行为空、占比图退化成明细表。
    if dim_col is None and not has_time:
        dim_col = next((c for c in columns if c not in metric_cols), None)
    if has_time and breakdown_col is None:
        dim_col = None
    # 维度列定下来之后再定位指标列，才能把维度/时间列从候选里排除。
    # 仅在「调用方知道指标物理列、只是被 SQL 起了别名」时才兜底；
    # 完全不知道指标是哪列时乱认一列，会把明细表渲染成语义不明的柱状图。
    if metric_cols:
        exclude = {c for c in (dim_col, breakdown_col, time_col if has_time else None) if c}
        metric_cols = _resolve_metric_cols(columns, rows, metric_cols, exclude)

    # ---- 趋势：指标 × 时间（带维度时按维度分线） ----
    if intent in ("trend", "compare") and has_time and metric_cols:
        spec.type = "line"
        if breakdown_col:
            xs, series = _pivot_by_dim(rows, time_col, breakdown_col, metric_cols[0])
            spec.x = xs
            spec.series = [{"name": k, "data": v} for k, v in series.items()]
            if compare and compare.get("rows") and len(spec.series) <= 8:
                # 对比期与本期日期标签是错开的（同比差一年、环比差一个周期），
                # 按日期对齐必然全落空，得按周期内位置对齐。
                _, cseries = _pivot_by_dim(compare["rows"], time_col, breakdown_col,
                                           metric_cols[0], x_len=len(xs))
                label = compare.get("label", "前值")
                for k, v in cseries.items():
                    if k in series:
                        spec.series.append({"name": f"{label}·{k}", "data": v,
                                            "_dashed": True})
                spec.compare_label = label
            return spec
        spec.x = [str(r.get(time_col)) for r in rows]
        spec.series = [{"name": mc, "data": [r.get(mc) for r in rows]} for mc in metric_cols]
        if compare and compare.get("rows"):
            for mc in metric_cols[:2]:
                spec.series.append({"name": f"{compare.get('label','前值')}·{mc}",
                                    "data": _align_series(compare["rows"], len(spec.x),
                                                          time_col, mc),
                                    "_dashed": True})
            spec.compare_label = compare.get("label", "")
        return spec

    # ---- 排行 TopN：维度 × 单指标 ----
    if intent == "ranking" and dim_col and metric_cols:
        items = share_breakdown(rows, dim_col, metric_cols[0], top=15)
        horizontal = len(items) > 6 or intent == "ranking"
        spec.type = "hbar" if horizontal else "bar"
        spec.x = [i["name"] for i in items]
        spec.series = [{"name": metric_cols[0], "data": [i["value"] for i in items]}]
        return spec

    # ---- 占比：维度 × 单指标 → 饼图 ----
    if intent == "share" and dim_col and metric_cols:
        items = share_breakdown(rows, dim_col, metric_cols[0], top=10)
        spec.type = "pie"
        spec.pies = items
        return spec

    # ---- 维度对比 → 柱状 ----
    if dim_col and metric_cols and not has_time:
        items = share_breakdown(rows, dim_col, metric_cols[0], top=15)
        spec.type = "hbar" if len(items) > 6 else "bar"
        spec.x = [i["name"] for i in items]
        spec.series = [{"name": mc, "data": [
            next((it["value"] for it in share_breakdown(rows, dim_col, mc, top=15)
                  if it["name"] == i["name"]), None) for i in items]}
            for mc in metric_cols[:3]]
        return spec

    # ---- 单指标时点（有/无对比）→ 数值卡片 ----
    if len(rows) <= 3 and metric_cols:
        spec.type = "stat_card"
        for mc in metric_cols[:4]:
            st = series_stats([r.get(mc) for r in rows])
            card = {"label": mc, "value": st.get("sum"), "unit": unit, "sub": ""}
            if compare and compare.get("rows"):
                prev = series_stats([r.get(mc) for r in compare["rows"]]).get("sum")
                from app.agent.skills.compute import pct_change
                p = pct_change(st.get("sum"), prev)
                card["prev"] = prev
                card["pct"] = p
                card["trend"] = ("up" if (p or 0) > 0 else "down") if p is not None else "flat"
                card["sub"] = f"对比{compare.get('label', '上期')}: {prev}（{'+' if (p or 0) >= 0 else ''}{p}%）" if p is not None else ""
            spec.cards.append(card)
        return spec

    # ---- 兜底：明细表 ----
    spec.type = "table"
    spec.columns = columns[:20]
    spec.rows = [[r.get(c) for c in spec.columns] for r in rows[:100]]
    return spec
