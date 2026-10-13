"""数仓规范服务（PRD 3.3E）：规范加载、代理上下文注入、结构化规则机器校验。

结构化规则（rules 为启用的 key 列表，机器可校验）：
- layer_prefix_required   表名必须带分层前缀（ods_/dwd_/dws_/ads_/dim_）
- partition_required      DDL 必须声明分区字段（PARTITIONED BY）
- comment_required        表必须有中文 COMMENT
- partition_filter_required 加工 SQL 必须含分区过滤（${ds} 或 ds =）
- no_reverse_layer_ref    禁止反向跨层引用（低层表不得引用更高层的表作来源）
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DwStandard

RULE_DEFS: dict[str, str] = {
    "layer_prefix_required": "表名必须带分层前缀（ods_/dwd_/dws_/ads_/dim_）",
    "partition_required": "DDL 必须声明分区字段（PARTITIONED BY）",
    "comment_required": "表必须有中文 COMMENT",
    "partition_filter_required": "加工 SQL 必须含分区过滤（${ds} 或 ds 条件）",
    "no_reverse_layer_ref": "禁止反向跨层引用（如 DWD 不得引用 DWS/ADS 作为来源）",
}

_LAYER_ORDER = {"ods": 0, "dwd": 1, "dim": 1, "dws": 2, "ads": 3}
_LAYER_PREFIXES = tuple(_LAYER_ORDER)


async def load_enabled(db: AsyncSession) -> list[DwStandard]:
    return list((await db.execute(
        select(DwStandard).where(DwStandard.enabled.is_(True))
        .order_by(DwStandard.updated_at.desc()))).scalars())


def prompt_text(standards: list[DwStandard], max_chars: int = 4000) -> str:
    """注入代理上下文的规范文本（Markdown 正文 + 生效中的机器校验规则清单）。"""
    if not standards:
        return ""
    parts: list[str] = []
    for s in standards:
        body = (s.content or "").strip()
        rule_labels = [RULE_DEFS.get(r, r) for r in (s.rules or [])]
        seg = f"### {s.name}\n{body}" if body else f"### {s.name}"
        if rule_labels:
            seg += "\n机器校验项：" + "；".join(rule_labels)
        parts.append(seg)
    text = "\n\n".join(parts)
    return text[:max_chars]


def check_rules(standards: list[DwStandard], *, table_name: str = "", layer: str = "",
                ddl: str = "", etl_sql: str = "",
                source_layers: dict[str, str] | None = None) -> list[str]:
    """按启用规范的结构化开关逐项校验，返回违规定位（含规范名与条款）。"""
    violations: list[str] = []
    active: list[tuple[str, str]] = []   # (标准名, rule key)
    for s in standards:
        for r in (s.rules or []):
            if r in RULE_DEFS:
                active.append((s.name, r))
    if not active:
        return violations

    name_l = (table_name or "").lower()
    layer_l = (layer or "").lower()
    ddl_l = ddl.lower()
    sql_l = etl_sql.lower()

    def hit(rule: str) -> list[str]:
        return [sname for sname, r in active if r == rule]

    for sname in hit("layer_prefix_required"):
        if name_l and not name_l.startswith(_LAYER_PREFIXES):
            violations.append(f"规范「{sname}」：表名 {table_name} 未带分层前缀"
                              f"（{RULE_DEFS['layer_prefix_required']}）")
    for sname in hit("partition_required"):
        if ddl.strip() and "partitioned by" not in ddl_l:
            violations.append(f"规范「{sname}」：{table_name or 'DDL'} 未声明分区字段"
                              f"（{RULE_DEFS['partition_required']}）")
    for sname in hit("comment_required"):
        if ddl.strip() and not re.search(r"comment\s*['\"][^'\"]*[一-鿿]", ddl, re.I):
            violations.append(f"规范「{sname}」：{table_name or 'DDL'} 缺少中文 COMMENT"
                              f"（{RULE_DEFS['comment_required']}）")
    for sname in hit("partition_filter_required"):
        if etl_sql.strip() and "${ds}" not in etl_sql \
                and not re.search(r"\b(ds|dt|stat_date)\s*=", sql_l):
            violations.append(f"规范「{sname}」：{table_name or '加工 SQL'} 未含分区过滤"
                              f"（{RULE_DEFS['partition_filter_required']}）")
    for sname in hit("no_reverse_layer_ref"):
        my_order = _LAYER_ORDER.get(layer_l)
        if my_order is None or not source_layers:
            continue
        for src, src_layer in source_layers.items():
            if _LAYER_ORDER.get((src_layer or "").lower(), -1) > my_order:
                violations.append(
                    f"规范「{sname}」：{table_name or layer_l}（{layer_l}）反向引用了更高层来源表 "
                    f"{src}（{src_layer}）（{RULE_DEFS['no_reverse_layer_ref']}）")
    return violations


# 导入文件时的结构化开关启发式建议（PRD 3.3E：按规范正文内容建议勾选）
_RULE_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("layer_prefix_required", ("分层前缀", "命名规范", "前缀", "ods_", "dwd_", "dws_", "ads_")),
    ("partition_required", ("分区表", "声明分区", "partitioned", "分区字段")),
    ("comment_required", ("注释", "comment", "中文说明")),
    ("partition_filter_required", ("分区过滤", "分区裁剪", "${ds}", "ds=")),
    ("no_reverse_layer_ref", ("跨层", "反向引用", "单向", "层级引用")),
]


def suggest_rules(content: str) -> list[str]:
    """按规范正文关键词建议结构化开关（人工确认后才生效）。"""
    text = (content or "").lower()
    out: list[str] = []
    for key, hints in _RULE_HINTS:
        if any(h.lower() in text for h in hints):
            out.append(key)
    return out
