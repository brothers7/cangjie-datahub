"""用户 / 权限 / 脱敏规则。"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, TimestampMixin, UTCDateTime, UUIDMixin


class Role(str, enum.Enum):
    ADMIN = "admin"        # 数据管理员：数据源/图谱/LLM 配置/用户管理
    ANALYST = "analyst"    # 分析师：问答 + 图谱查看
    VIEWER = "viewer"      # 普通业务用户：问答 + 报表


class UserStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class User(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(sa.String(256))
    display_name: Mapped[str] = mapped_column(sa.String(64), default="")
    role: Mapped[Role] = mapped_column(
        sa.Enum(Role, native_enum=False, length=16), default=Role.VIEWER
    )
    status: Mapped[UserStatus] = mapped_column(
        sa.Enum(UserStatus, native_enum=False, length=16), default=UserStatus.ACTIVE
    )
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # 登录失败限流（PRD 4.4）：连续失败计数 + 锁定截止时刻，成功登录清零
    failed_login_count: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class DatasourcePermission(UUIDMixin, TimestampMixin, Base):
    """用户 × 数据源 查询权限。admin 隐式拥有全部权限。"""
    __tablename__ = "datasource_permissions"
    __table_args__ = (sa.UniqueConstraint("user_id", "datasource_id", name="uq_user_ds"),)

    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    datasource_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("datasources.id", ondelete="CASCADE"), index=True
    )
    can_query: Mapped[bool] = mapped_column(default=True)


class MaskStrategy(str, enum.Enum):
    MASK_PARTIAL = "mask_partial"  # 保留首尾，中间打码
    MASK_HASH = "mask_hash"       # 哈希脱敏
    MASK_NULL = "mask_null"       # 置空


class MaskingRule(UUIDMixin, TimestampMixin, Base):
    """敏感字段脱敏规则（按列名模式匹配，支持 * 通配）。"""
    __tablename__ = "masking_rules"

    datasource_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("datasources.id", ondelete="CASCADE"), index=True
    )
    column_pattern: Mapped[str] = mapped_column(sa.String(128))  # 如 *phone* / id_card
    strategy: Mapped[MaskStrategy] = mapped_column(
        sa.Enum(MaskStrategy, native_enum=False, length=16), default=MaskStrategy.MASK_PARTIAL
    )
    enabled: Mapped[bool] = mapped_column(default=True)
