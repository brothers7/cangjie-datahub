"""通用小工具。"""
from __future__ import annotations


def like_escape(s: str) -> str:
    """转义 LIKE/ILIKE 模式中的通配符（PRD 4.4）：用户输入仅按字面匹配。

    配合 SQLAlchemy 的 ``escape="\\"`` 使用，防 `%`/`_` 注入造成意外全量匹配。
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def like_contains(s: str) -> str:
    """生成「包含」语义的转义后 LIKE 模式（两侧 %% 为真实通配）。"""
    return f"%{like_escape(s)}%"
