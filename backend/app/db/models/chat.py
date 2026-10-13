"""对话 / 消息 / 查询记录 / 缓存 / 报表 / 用户记忆画像。"""
from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, JSONType, TimestampMixin, UTCDateTime, UUIDMixin, UUIDType


def utcnow() -> datetime:
    return datetime.now(UTC)


class ConversationStatus(str, enum.Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class Conversation(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(sa.String(255), default="新会话")
    datasource_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), index=True)
    # 会话级模型覆盖：用户在本会话选定的 LLM 配置；None = 跟随场景绑定/全局默认
    llm_config_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), index=True)
    # 短时记忆：当前指标/维度/时间窗/上次SQL摘要/对比基准
    short_term_state: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[ConversationStatus] = mapped_column(
        sa.Enum(ConversationStatus, native_enum=False, length=16), default=ConversationStatus.ACTIVE
    )
    message_count: Mapped[int] = mapped_column(default=0)
    last_active_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class MessageRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class Message(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[MessageRole] = mapped_column(sa.Enum(MessageRole, native_enum=False, length=16))
    content: Mapped[str] = mapped_column(sa.Text, default="")
    # 结构化产物：matched_metrics/dimensions/time_range/sql/chart_spec/step_trace/report 等
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)


class QueryStatus(str, enum.Enum):
    SUCCESS = "success"
    EMPTY = "empty"
    BLOCKED = "blocked"
    ERROR = "error"


class QueryRecord(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "query_records"

    user_id: Mapped[uuid.UUID] = mapped_column(UUIDType(), index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), index=True)
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType())
    datasource_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), index=True)
    question: Mapped[str] = mapped_column(sa.Text)
    rewritten_question: Mapped[str] = mapped_column(sa.Text, default="")
    matched_metric_ids: Mapped[list] = mapped_column(JSONType, default=list)
    used_dimensions: Mapped[list] = mapped_column(JSONType, default=list)
    time_range: Mapped[dict] = mapped_column(JSONType, default=dict)
    sql_text: Mapped[str] = mapped_column(sa.Text, default="")
    sql_hash: Mapped[str] = mapped_column(sa.String(64), default="", index=True)
    status: Mapped[QueryStatus] = mapped_column(
        sa.Enum(QueryStatus, native_enum=False, length=16), default=QueryStatus.SUCCESS
    )
    row_count: Mapped[int] = mapped_column(default=0)
    duration_ms: Mapped[int] = mapped_column(default=0)
    cache_hit: Mapped[bool] = mapped_column(default=False)
    error_brief: Mapped[str] = mapped_column(sa.Text, default="")
    chart_type: Mapped[str] = mapped_column(sa.String(32), default="")


class QueryCache(UUIDMixin, Base):
    __tablename__ = "query_cache"

    cache_key: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True)
    result: Mapped[dict] = mapped_column(JSONType)  # {columns, rows, stats}
    chart_spec: Mapped[dict | None] = mapped_column(JSONType, default=None)
    answer_text: Mapped[str] = mapped_column(sa.Text, default="")
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    hit_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ReportStatus(str, enum.Enum):
    ACTIVE = "active"
    DELETED = "deleted"


class Report(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "reports"

    user_id: Mapped[uuid.UUID] = mapped_column(UUIDType(), index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), index=True)
    query_record_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType())
    title: Mapped[str] = mapped_column(sa.String(255), default="")
    content_md: Mapped[str] = mapped_column(sa.Text, default="")
    sections: Mapped[list] = mapped_column(JSONType, default=list)
    status: Mapped[ReportStatus] = mapped_column(
        sa.Enum(ReportStatus, native_enum=False, length=16), default=ReportStatus.ACTIVE
    )
    view_count: Mapped[int] = mapped_column(default=0)


class ReportTemplate(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "report_templates"

    name: Mapped[str] = mapped_column(sa.String(128))
    structure: Mapped[dict] = mapped_column(JSONType, default=dict)
    source: Mapped[str] = mapped_column(sa.String(16), default="manual")  # manual/reflection
    usage_count: Mapped[int] = mapped_column(default=0)
    created_from_user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType())


class UserMemoryProfile(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "user_memory_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(UUIDType(), unique=True, index=True)
    habit_summary: Mapped[str] = mapped_column(sa.Text, default="")  # 反思生成的习惯总结
    favorite_metrics: Mapped[list] = mapped_column(JSONType, default=list)   # [{metric_id, name, count}]
    favorite_dimensions: Mapped[list] = mapped_column(JSONType, default=list)
    preferred_chart: Mapped[str] = mapped_column(sa.String(32), default="")
