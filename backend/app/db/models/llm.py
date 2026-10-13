"""LLM 配置 / 场景绑定 / 调用日志。"""
from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, TimestampMixin, UTCDateTime, UUIDMixin, UUIDType, utcnow


class Protocol(str, enum.Enum):
    OPENAI = "openai"          # /v1/chat/completions 兼容（OpenAI/DeepSeek/GLM/Kimi/vLLM…）
    ANTHROPIC = "anthropic"    # /v1/messages


class ModelKind(str, enum.Enum):
    CHAT = "chat"
    EMBEDDING = "embedding"


class LlmScene(str, enum.Enum):
    QUESTION_REWRITE = "question_rewrite"
    INTENT_ROUTE = "intent_route"
    ENTITY_LINK_ASSIST = "entity_link_assist"
    SQL_GENERATE = "sql_generate"
    SQL_FIX = "sql_fix"
    ANOMALY_ANALYSIS = "anomaly_analysis"
    REPORT_GENERATE = "report_generate"
    CHAT_ANSWER = "chat_answer"
    TABLE_QA = "table_qa"          # 库表使用答疑（PRD 3.3A）
    ETL_DESIGN = "etl_design"      # ETL 需求开发助手（PRD 3.3B）
    CUSTOM_SKILL = "custom_skill"  # 自定义 Skill（Prompt 型，PRD 3.3C）
    MODELING_AGENT = "modeling_agent"  # AI 建模工程师（内置编码代理，PRD 3.3D-6）
    GOVERNANCE = "governance"      # AI 数据治理报告（PRD 3.3F）
    REFLECTION = "reflection"
    EMBED_QUERY = "embed_query"


class LlmHealthStatus(str, enum.Enum):
    HEALTHY = "healthy"
    ERROR = "error"
    UNCHECKED = "unchecked"


class LlmConfig(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "llm_configs"

    name: Mapped[str] = mapped_column(sa.String(128), unique=True)
    protocol: Mapped[Protocol] = mapped_column(sa.Enum(Protocol, native_enum=False, length=16))
    kind: Mapped[ModelKind] = mapped_column(
        sa.Enum(ModelKind, native_enum=False, length=16), default=ModelKind.CHAT
    )
    endpoint: Mapped[str] = mapped_column(sa.String(512))  # 如 https://api.openai.com/v1
    api_key_cipher: Mapped[str] = mapped_column(sa.Text, default="")
    model: Mapped[str] = mapped_column(sa.String(128))
    temperature: Mapped[float] = mapped_column(default=0.2)
    max_tokens: Mapped[int] = mapped_column(default=4096)
    top_p: Mapped[float] = mapped_column(default=1.0)
    timeout_sec: Mapped[int] = mapped_column(default=30)
    retry_count: Mapped[int] = mapped_column(default=2)
    stream_enabled: Mapped[bool] = mapped_column(default=True)
    is_default: Mapped[bool] = mapped_column(default=False)
    is_default_embedding: Mapped[bool] = mapped_column(default=False)
    fallback_config_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("llm_configs.id"))
    switch_policy: Mapped[str] = mapped_column(sa.String(16), default="immediate")  # immediate/new_session
    status: Mapped[LlmHealthStatus] = mapped_column(
        sa.Enum(LlmHealthStatus, native_enum=False, length=16), default=LlmHealthStatus.UNCHECKED
    )
    status_message: Mapped[str] = mapped_column(sa.Text, default="")
    health_latency_ms: Mapped[int | None] = mapped_column(default=None)
    last_health_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class LlmSceneBinding(UUIDMixin, TimestampMixin, Base):
    """场景 × 配置绑定：不同环节可用不同模型（轻模型做改写、强模型做 SQL/报表）。"""
    __tablename__ = "llm_scene_bindings"
    __table_args__ = (sa.UniqueConstraint("scene", name="uq_scene_binding"),)

    scene: Mapped[LlmScene] = mapped_column(sa.Enum(LlmScene, native_enum=False, length=32))
    config_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("llm_configs.id", ondelete="CASCADE"))


class CallStatus(str, enum.Enum):
    OK = "ok"
    TIMEOUT = "timeout"
    ERROR = "error"
    DEGRADED = "degraded"  # 主配置失败走了降级配置


class LlmCallLog(UUIDMixin, Base):
    __tablename__ = "llm_call_logs"

    config_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), index=True)
    scene: Mapped[str] = mapped_column(sa.String(32), default="", index=True)
    protocol: Mapped[str] = mapped_column(sa.String(16), default="")
    model: Mapped[str] = mapped_column(sa.String(128), default="")
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    latency_ms: Mapped[int] = mapped_column(default=0)
    status: Mapped[CallStatus] = mapped_column(sa.Enum(CallStatus, native_enum=False, length=16))
    error_brief: Mapped[str] = mapped_column(sa.Text, default="")
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), default=None)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, index=True
    )
