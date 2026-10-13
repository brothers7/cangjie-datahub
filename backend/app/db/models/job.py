"""扫描任务 / 审计日志。"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, JSONType, TimestampMixin, UTCDateTime, UUIDMixin, UUIDType, utcnow


class ScanMode(str, enum.Enum):
    FULL = "full"
    INCREMENTAL = "incremental"


class ScanJobStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


class ScanJob(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "scan_jobs"

    datasource_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("datasources.id", ondelete="CASCADE"), index=True
    )
    mode: Mapped[ScanMode] = mapped_column(sa.Enum(ScanMode, native_enum=False, length=16))
    triggered_by: Mapped[str] = mapped_column(sa.String(64), default="manual")  # user:<id> / cron
    status: Mapped[ScanJobStatus] = mapped_column(
        sa.Enum(ScanJobStatus, native_enum=False, length=16), default=ScanJobStatus.RUNNING
    )
    tables_total: Mapped[int] = mapped_column(default=0)
    tables_changed: Mapped[int] = mapped_column(default=0)
    entities_created: Mapped[int] = mapped_column(default=0)
    entities_updated: Mapped[int] = mapped_column(default=0)
    entities_skipped_manual: Mapped[int] = mapped_column(default=0)  # 人工保护跳过数
    progress: Mapped[int] = mapped_column(default=0)  # 0~100
    error_log: Mapped[list] = mapped_column(JSONType, default=list)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AuditLog(UUIDMixin, Base):
    __tablename__ = "audit_logs"

    user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), index=True)
    username: Mapped[str] = mapped_column(sa.String(64), default="")
    action: Mapped[str] = mapped_column(sa.String(64), index=True)  # login/datasource.create/...
    resource_type: Mapped[str] = mapped_column(sa.String(32), default="")
    resource_id: Mapped[str] = mapped_column(sa.String(64), default="")
    detail: Mapped[dict] = mapped_column(JSONType, default=dict)  # 敏感字段已剔除
    ip: Mapped[str] = mapped_column(sa.String(64), default="")
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, index=True
    )
