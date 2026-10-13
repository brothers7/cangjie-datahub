"""数据校验 Skill：空数据 / 脏数据过滤 / 值域 / 维度匹配（PRD 3.6）。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    ok: bool = True
    empty: bool = False
    dirty_dropped: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "empty": self.empty, "dirty_dropped": self.dirty_dropped,
                "warnings": self.warnings, "errors": self.errors}


def validate_result(columns: list[str], rows: list[dict], *,
                    metric_cols: list[str] | None = None,
                    value_ranges: dict[str, list] | None = None,
                    expected_dims: list[str] | None = None) -> tuple[list[dict], ValidationResult]:
    """返回 (清洗后的行, 校验结果)。"""
    vr = ValidationResult()
    if not rows:
        vr.empty = True
        vr.ok = False
        vr.warnings.append("查询结果为空：建议检查时间范围是否超出数据覆盖区间，或口径条件是否过严")
        return [], vr

    metric_cols = metric_cols or []
    expected_dims = expected_dims or []
    for d in expected_dims:
        if d not in columns:
            vr.warnings.append(f"结果缺少维度列「{d}」，可能该维度在目标表中不可用")

    cleaned: list[dict] = []
    for r in rows:
        dirty = False
        for mc in metric_cols:
            v = r.get(mc)
            if v is not None:
                try:
                    fv = float(v)
                    if fv != fv:  # NaN
                        dirty = True
                except (TypeError, ValueError):
                    dirty = True
        if not dirty:
            cleaned.append(r)
        else:
            vr.dirty_dropped += 1
    if vr.dirty_dropped:
        vr.warnings.append(f"过滤脏数据 {vr.dirty_dropped} 行（指标列含非数值/缺失）")
    if rows and not cleaned:
        # 全行被判脏 → 等同空结果，走空结果提示路径而非展示"共 0 行"
        vr.empty = True
        vr.ok = False
        vr.warnings.append("查询结果全部行均为无效数值，请检查指标列口径")
    return cleaned, vr

    # 值域校验（来自 KG 指标约束）
    for col, rng in (value_ranges or {}).items():
        if not (isinstance(rng, list) and len(rng) == 2) or col not in columns:
            continue
        lo, hi = rng
        out_n = 0
        for r in cleaned:
            try:
                v = float(r.get(col))
                if v < lo or v > hi:
                    out_n += 1
            except (TypeError, ValueError):
                continue
        if out_n:
            vr.warnings.append(f"指标「{col}」有 {out_n} 行超出知识图谱值域 [{lo}, {hi}]，请复核口径")
    return cleaned, vr
