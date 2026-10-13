"""元数据结构哈希：增量扫描的差异检测。"""
from __future__ import annotations

import hashlib

from app.connectors.base import ColumnMeta, TableMeta


def column_hash(c: ColumnMeta) -> str:
    raw = "|".join([
        _norm_name(c.column_name), _norm_type(c.data_type), (c.comment or "").strip(),
        "1" if c.is_primary_key else "0", "1" if c.nullable else "0",
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def table_hash(tm: TableMeta) -> str:
    parts = [_norm_name(tm.table_name), (tm.comment or "").strip()]
    for c in sorted(tm.columns, key=lambda x: x.column_name):
        parts.append(column_hash(c))
    raw = ";".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _norm_name(s: str) -> str:
    return (s or "").strip().lower()


def _norm_type(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "")
