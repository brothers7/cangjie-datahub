"""异常分析 Skill：大涨大跌 / 离群点 / 数据缺失识别（PRD 3.6）。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.agent.skills.compute import _num, series_stats


@dataclass
class AnomalyPoint:
    index: int
    x: str
    value: float
    direction: str  # up / down
    method: str     # zscore / iqr / dod(环比突变)
    severity: str   # high / medium
    note: str = ""


@dataclass
class AnomalyReport:
    points: list[AnomalyPoint] = field(default_factory=list)
    missing_dates: list[str] = field(default_factory=list)

    @property
    def has_anomaly(self) -> bool:
        return bool(self.points or self.missing_dates)

    def to_dict(self) -> dict:
        return {
            "points": [{"index": p.index, "x": p.x, "value": p.value,
                        "direction": p.direction, "method": p.method,
                        "severity": p.severity, "note": p.note} for p in self.points],
            "missing_dates": self.missing_dates,
        }


def detect_anomalies(x_labels: list[str], values: list,
                     dod_threshold: float = 30.0) -> AnomalyReport:
    """对单个数值序列做异常检测：3σ + IQR + 日环比突变。"""
    report = AnomalyReport()
    nums = [_num(v) for v in values]
    pairs = [(i, xv, nv) for i, (xv, nv) in enumerate(zip(x_labels, nums)) if nv is not None]
    if len(pairs) < 3:
        return report

    vals = [p[2] for p in pairs]
    st = series_stats(vals)
    mean, std = st.get("mean"), st.get("std") or 0.0
    q = sorted(vals)
    n = len(q)
    q1, q3 = q[n // 4], q[(3 * n) // 4]
    iqr = q3 - q1

    for i, xv, v in pairs:
        z = abs(v - mean) / std if std > 1e-9 else 0
        if z >= 3:
            report.points.append(AnomalyPoint(
                i, str(xv), v, "up" if v > mean else "down", "zscore", "high",
                f"偏离均值 {z:.1f} 个标准差"))
        elif iqr > 1e-9 and (v < q1 - 1.5 * iqr or v > q3 + 1.5 * iqr):
            report.points.append(AnomalyPoint(
                i, str(xv), v, "up" if v > q3 else "down", "iqr", "medium",
                f"超出四分位区间 [{round(q1 - 1.5 * iqr, 2)}, {round(q3 + 1.5 * iqr, 2)}]"))

    # 日环比突变
    prev = None
    for i, xv, v in pairs:
        if prev is not None and prev != 0:
            chg = (v - prev) / abs(prev) * 100
            if abs(chg) >= dod_threshold and not math.isnan(chg):
                report.points.append(AnomalyPoint(
                    i, str(xv), v, "up" if chg > 0 else "down", "dod",
                    "high" if abs(chg) >= 2 * dod_threshold else "medium",
                    f"较前值{('上涨' if chg > 0 else '下跌')} {abs(chg):.1f}%"))
        prev = v

    # 去重（同一点多方法命中保留最高严重度）
    dedup: dict[tuple, AnomalyPoint] = {}
    sev_rank = {"high": 2, "medium": 1}
    for p in report.points:
        k = (p.index, p.direction)
        if k not in dedup or sev_rank[p.severity] > sev_rank[dedup[k].severity]:
            dedup[k] = p
    report.points = sorted(dedup.values(), key=lambda p: p.index)
    return report


def find_missing_dates(x_labels: list[str]) -> list[str]:
    """时间序列缺日检测（x 为 YYYY-MM-DD）。"""
    from datetime import date, timedelta

    missing: list[str] = []
    try:
        ds = [date.fromisoformat(str(x)) for x in x_labels]
    except ValueError:
        return missing
    if len(ds) < 2:
        return missing
    have = set(ds)
    cur, end = ds[0], ds[-1]
    while cur < end:
        cur += timedelta(days=1)
        if cur not in have:
            missing.append(cur.isoformat())
    return missing[:50]
