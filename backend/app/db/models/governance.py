"""数仓规范与 AI 数据治理（PRD 3.3E / 3.3F）。"""
from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, JSONType, TimestampMixin, UTCDateTime, UUIDMixin, utcnow


class DwStandard(UUIDMixin, TimestampMixin, Base):
    """数仓规范：Markdown 正文（人读 + 注入代理上下文）+ 结构化规则开关（机器校验）。"""
    __tablename__ = "dw_standards"

    name: Mapped[str] = mapped_column(sa.String(128), index=True)
    content: Mapped[str] = mapped_column(sa.Text, default="")     # Markdown 规范正文
    rules: Mapped[list] = mapped_column(JSONType, default=list)   # 启用的结构化规则 key
    enabled: Mapped[bool] = mapped_column(default=True)
    updated_by: Mapped[str] = mapped_column(sa.String(128), default="")


class GovernanceRun(UUIDMixin, Base):
    """治理巡检快照：一次巡检的治理项集合与 AI 报告，留档可回看对比。"""
    __tablename__ = "governance_runs"

    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"), index=True)
    datasource_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    findings: Mapped[list] = mapped_column(JSONType, default=list)
    # finding: {domain, level, object, title, evidence, suggestion, task_id?}
    report: Mapped[str] = mapped_column(sa.Text, default="")      # LLM 治理报告（可空）
    stats: Mapped[dict] = mapped_column(JSONType, default=dict)   # {domain: count}
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
