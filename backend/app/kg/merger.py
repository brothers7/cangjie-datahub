"""图谱增量合并保护：人工口径永不覆盖（PRD 3.2.5 / 4.3）。

三条规则：
1. source=manual 实体：扫描永不触碰
2. source=auto/hybrid 实体：仅更新 edited_fields 之外的自动字段
3. 人工废弃/合并（tombstone）的实体：重扫不复活
"""
from __future__ import annotations

from app.db.models.metadata import SourceKind


def is_tombstoned(entity) -> bool:
    status = getattr(entity, "status", None)
    return status is not None and status.value in ("deprecated", "merged")


def is_fully_protected(entity) -> bool:
    """纯人工实体：扫描完全跳过。"""
    return entity.source == SourceKind.MANUAL


def apply_auto_merge(entity, incoming: dict, auto_fields: list[str]) -> bool:
    """把自动扫描的 incoming 字段合并进实体，跳过人工编辑过的字段。

    返回是否有变更。会把 source 从 auto 升级为 hybrid（当存在人工编辑字段时）。
    """
    edited = set(entity.edited_fields or [])
    changed = False
    for f in auto_fields:
        if f in edited:
            continue
        new_val = incoming.get(f)
        old_val = getattr(entity, f, None)
        if new_val is not None and new_val != old_val:
            setattr(entity, f, new_val)
            changed = True
    if changed and entity.source == SourceKind.AUTO and edited:
        entity.source = SourceKind.HYBRID
    return changed


def mark_edited(entity, field_names: list[str]) -> None:
    """人工编辑 API 调用：记录保护字段。"""
    edited = set(entity.edited_fields or [])
    edited.update(field_names)
    entity.edited_fields = sorted(edited)
    entity.source = SourceKind.HYBRID
