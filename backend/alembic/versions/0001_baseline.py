"""baseline：当前全部模型的 create_all 等价基线。

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-20

说明：
- 直接以 app.db.models.Base.metadata.create_all 建立全量表结构，
  与模型定义保持单一事实来源，后续变更走增量 revision。
- checkfirst=True：已存在的表自动跳过。
  因此老部署（库已由旧版 create_all / bootstrap 建表）既可以
  `alembic stamp head` 直接标记到基线（推荐），
  也可以安全地跑 `upgrade head`（不会重复建表）；全新库 upgrade 即建全表。
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from app.db.models import Base

    bind = op.get_bind()
    Base.metadata.create_all(bind, checkfirst=True)


def downgrade() -> None:
    from app.db.models import Base

    bind = op.get_bind()
    Base.metadata.drop_all(bind, checkfirst=True)
