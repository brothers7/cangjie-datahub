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


class ProbeRun(UUIDMixin, Base):
    """质量探查留档（PRD 3.3F）：一次探查的入参/统计/发现/执行者/耗时，可回看审计。"""
    __tablename__ = "probe_runs"

    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"), index=True)
    username: Mapped[str] = mapped_column(sa.String(128), default="")
    datasource_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    table_name: Mapped[str] = mapped_column(sa.String(255), default="", index=True)
    columns: Mapped[list] = mapped_column(JSONType, default=list)   # 探查字段
    params: Mapped[dict] = mapped_column(JSONType, default=dict)    # 入参（采样行数等）
    stats: Mapped[dict] = mapped_column(JSONType, default=dict)     # 列级统计
    findings: Mapped[list] = mapped_column(JSONType, default=list)  # 规则发现 + 建议
    duration_ms: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
