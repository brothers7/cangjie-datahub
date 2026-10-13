"""数据计算 Skill：环比/同比/占比/均值/增量/波动率（PRD 3.6）。"""
from __future__ import annotations

import math
from dataclasses import dataclass


def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def pct_change(cur, prev) -> float | None:
    """百分比变化：(cur-prev)/prev*100。"""
    c, p = _num(cur), _num(prev)
    if c is None or p is None or p == 0:
        return None
    return round((c - p) / abs(p) * 100, 2)


def delta(cur, prev):
    c, p = _num(cur), _num(prev)
    if c is None or p is None:
        return None
    return round(c - p, 4)


def safe_div(a, b):
    a_, b_ = _num(a), _num(b)
    if a_ is None or b_ in (None, 0):
        return None
    return round(a_ / b_, 6)


def series_stats(values: list) -> dict:
    nums = [v for v in (_num(x) for x in values) if v is not None]
    if not nums:
        return {"count": 0}
    n = len(nums)
    mean = sum(nums) / n
    var = sum((x - mean) ** 2 for x in nums) / n if n > 1 else 0.0
    return {
        "count": n, "sum": round(sum(nums), 4), "mean": round(mean, 4),
        "std": round(math.sqrt(var), 4), "min": min(nums), "max": max(nums),
        "median": round(sorted(nums)[n // 2], 4),
    }


@dataclass
class MetricCompare:
    name: str
    current: float | None
    previous: float | None
    delta: float | None
    pct: float | None  # 环比/同比百分比


def compare_rows(cur_rows: list[dict], prev_rows: list[dict],
                 metric_cols: list[str]) -> list[MetricCompare]:
    """当期/前期按指标列聚合对比。"""
    out: list[MetricCompare] = []
    for col in metric_cols:
        cur_v = _agg(cur_rows, col)
        prev_v = _agg(prev_rows, col)
        out.append(MetricCompare(col, cur_v, prev_v,
                                 delta(cur_v, prev_v), pct_change(cur_v, prev_v)))
    return out


def _agg(rows: list[dict], col: str) -> float | None:
    total = 0.0
    has = False
    for r in rows or []:
        v = _num(r.get(col))
        if v is not None:
            total += v
            has = True
    return round(total, 4) if has else None


def share_breakdown(rows: list[dict], dim_col: str, metric_col: str, top: int = 20) -> list[dict]:
    """按维度拆解 + 占比（饼图/排行）。"""
    agg: dict[str, float] = {}
    for r in rows or []:
        key = str(r.get(dim_col, "未知"))
        v = _num(r.get(metric_col))
        if v is not None:
            agg[key] = agg.get(key, 0.0) + v
    total = sum(agg.values())
    items = sorted(agg.items(), key=lambda kv: -kv[1])[:top]
    return [{"name": k, "value": round(v, 4),
             "share": round(v / total * 100, 2) if total else None} for k, v in items]


def volatility(values: list) -> float | None:
    """波动率 = std/mean*100。"""
    st = series_stats(values)
    if st.get("count", 0) < 2 or not st.get("mean"):
        return None
    return round(st["std"] / st["mean"] * 100, 2)
