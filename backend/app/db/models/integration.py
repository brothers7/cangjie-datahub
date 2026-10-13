"""外部集成与自定义 Skill（PRD 3.3B Azkaban 对接 / 3.3C 自定义 Skill 托管）。"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, JSONType, TimestampMixin, UTCDateTime, UUIDMixin, UUIDType, utcnow


class IntegrationStatus(str, enum.Enum):
    HEALTHY = "healthy"
    ERROR = "error"
    UNCHECKED = "unchecked"


class AzkabanConfig(UUIDMixin, TimestampMixin, Base):
    """Azkaban 连接配置（单行语义，取最新一条）。"""
    __tablename__ = "azkaban_configs"

    url: Mapped[str] = mapped_column(sa.String(512), default="")       # Web Server 地址
    username: Mapped[str] = mapped_column(sa.String(128), default="")
    password_cipher: Mapped[str] = mapped_column(sa.Text, default="")  # AES-256-GCM
    default_project: Mapped[str] = mapped_column(sa.String(255), default="data-agent-etl")
    verify_tls: Mapped[bool] = mapped_column(default=False)  # 校验 TLS 证书（PRD 4.4，默认关兼容自签名）
    status: Mapped[IntegrationStatus] = mapped_column(
        sa.Enum(IntegrationStatus, native_enum=False, length=16),
        default=IntegrationStatus.UNCHECKED)
    status_message: Mapped[str] = mapped_column(sa.Text, default="")


class AirflowConfig(UUIDMixin, TimestampMixin, Base):
    """Airflow 连接配置（PRD 3.3G，单行语义，取最新一条）。"""
    __tablename__ = "airflow_configs"

    url: Mapped[str] = mapped_column(sa.String(512), default="")       # REST API 地址
    username: Mapped[str] = mapped_column(sa.String(128), default="")
    password_cipher: Mapped[str] = mapped_column(sa.Text, default="")  # AES-256-GCM
    verify_tls: Mapped[bool] = mapped_column(default=False)  # 校验 TLS 证书（PRD 4.4）
    status: Mapped[IntegrationStatus] = mapped_column(
        sa.Enum(IntegrationStatus, native_enum=False, length=16),
        default=IntegrationStatus.UNCHECKED)
    status_message: Mapped[str] = mapped_column(sa.Text, default="")


class SchedulerRunStatus(str, enum.Enum):
    WAITING = "waiting"      # 等待依赖
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"      # 双重闸门未满足（待审核/数据源未开放写入），无写动作
    DEP_TIMEOUT = "dep_timeout"  # 依赖超过 24h 未成功


class SchedulerRun(UUIDMixin, Base):
    """内置调度引擎的运行实例（PRD 3.3G）：一次任务 × 一个业务日期。"""
    __tablename__ = "scheduler_runs"

    task_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("etl_tasks.id", ondelete="CASCADE"), index=True)
    ds: Mapped[str] = mapped_column(sa.String(16), index=True)         # 业务日期 yyyy-mm-dd
    slot: Mapped[str] = mapped_column(sa.String(8), default="")        # 触发时刻 HH:MM（定时去重键）
    status: Mapped[SchedulerRunStatus] = mapped_column(
        sa.Enum(SchedulerRunStatus, native_enum=False, length=16),
        default=SchedulerRunStatus.WAITING, index=True)
    trigger_type: Mapped[str] = mapped_column(sa.String(16), default="scheduled")  # scheduled/manual/retry
    message: Mapped[str] = mapped_column(sa.Text, default="")          # 阻塞/失败原因
    duration_ms: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class SkillType(str, enum.Enum):
    PROMPT = "prompt"   # 提示词模板型，走 LLM 网关
    PYTHON = "python"   # 代码型，沙箱子进程执行
    SCHEDULER = "scheduler"  # 调度平台适配（PRD 3.3D-4）：build_package 契约，只生成工程文件


class SkillStatus(str, enum.Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"


class EtlTaskStatus(str, enum.Enum):
    DRAFT = "draft"      # 草稿（可编辑）
    PUSHED = "pushed"    # 已推送 Azkaban


class ModelingSessionStatus(str, enum.Enum):
    ACTIVE = "active"        # 进行中（可继续对话修订）
    ACCEPTED = "accepted"    # 已接受建议并落地为任务
    ARCHIVED = "archived"


class EtlTaskSource(str, enum.Enum):
    CHAT = "chat"          # 对话 ETL 助手转入
    MANUAL = "manual"      # 手工建模
    AI = "ai"              # AI 辅助新建
    IMPORTED = "imported"  # 从 Azkaban 已有工程导入


class EtlTask(UUIDMixin, TimestampMixin, Base):
    """ETL 建模任务（PRD 3.3D 自动化建模模块的核心实体）。"""
    __tablename__ = "etl_tasks"

    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"), index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("conversations.id"), nullable=True)
    datasource_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("datasources.id"), nullable=True)
    name: Mapped[str] = mapped_column(sa.String(255), default="")       # 任务名（默认目标表名）
    target_table: Mapped[str] = mapped_column(sa.String(255), default="")
    layer: Mapped[str] = mapped_column(sa.String(16), default="")
    dialect: Mapped[str] = mapped_column(sa.String(32), default="")
    schedule: Mapped[str] = mapped_column(sa.String(255), default="")   # 调度建议（人读）
    cron: Mapped[str] = mapped_column(sa.String(64), default="")        # Azkaban cron 表达式
    depends_on: Mapped[list] = mapped_column(JSONType, default=list)    # 依赖任务 id 列表（任务级依赖）
    sched_enabled: Mapped[bool] = mapped_column(default=False)          # 启用内置调度（PRD 3.3G）
    approved_by: Mapped[str] = mapped_column(sa.String(64), default="")  # 内置调度执行审核人
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ddl: Mapped[str] = mapped_column(sa.Text, default="")
    etl_sql: Mapped[str] = mapped_column(sa.Text, default="")
    source_tables: Mapped[list] = mapped_column(JSONType, default=list)
    field_mapping: Mapped[list] = mapped_column(JSONType, default=list)
    notes: Mapped[str] = mapped_column(sa.Text, default="")
    warnings: Mapped[list] = mapped_column(JSONType, default=list)
    status: Mapped[EtlTaskStatus] = mapped_column(
        sa.Enum(EtlTaskStatus, native_enum=False, length=16), default=EtlTaskStatus.DRAFT)
    source: Mapped[EtlTaskSource] = mapped_column(
        sa.Enum(EtlTaskSource, native_enum=False, length=16), default=EtlTaskSource.MANUAL)
    azkaban_project: Mapped[str] = mapped_column(sa.String(255), default="")
    azkaban_flow: Mapped[str] = mapped_column(sa.String(255), default="")
    pushed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    modeling_session_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("modeling_sessions.id"), nullable=True)   # 来源建模会话（AI 建议落地）


class ModelingSession(UUIDMixin, TimestampMixin, Base):
    """AI 建模建议会话（PRD 3.3D-1）：需求 → 方案 → 对话修订 → 接受落地。"""
    __tablename__ = "modeling_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"), index=True)
    datasource_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("datasources.id"), nullable=True)
    title: Mapped[str] = mapped_column(sa.String(255), default="")
    requirement: Mapped[str] = mapped_column(sa.Text, default="")     # 首条需求原文
    llm_config_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), nullable=True)  # 会话级模型选择（PRD 3.3D-1）
    status: Mapped[ModelingSessionStatus] = mapped_column(
        sa.Enum(ModelingSessionStatus, native_enum=False, length=16),
        default=ModelingSessionStatus.ACTIVE)


class ModelingMessage(UUIDMixin, Base):
    """建模会话消息：assistant 消息可携带结构化方案快照（proposal）。"""
    __tablename__ = "modeling_messages"

    session_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("modeling_sessions.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(default=0)
    role: Mapped[str] = mapped_column(sa.String(16), default="user")  # user / assistant
    content: Mapped[str] = mapped_column(sa.Text, default="")         # 人读内容/方案摘要
    proposal: Mapped[dict | None] = mapped_column(JSONType, nullable=True)  # 结构化方案
    steps: Mapped[list] = mapped_column(JSONType, default=list)       # 代理步骤留档
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class EtlTaskVersion(UUIDMixin, Base):
    """任务版本快照：改动 DDL/加工 SQL 前留档，可回滚。"""
    __tablename__ = "etl_task_versions"

    task_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("etl_tasks.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(default=1)
    ddl: Mapped[str] = mapped_column(sa.Text, default="")
    etl_sql: Mapped[str] = mapped_column(sa.Text, default="")
    saved_by: Mapped[str] = mapped_column(sa.String(128), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class CustomSkill(UUIDMixin, TimestampMixin, Base):
    """自定义 Skill（zip 包内容入库：files = {文件名: 文本内容}）。"""
    __tablename__ = "custom_skills"
    __table_args__ = (
        sa.UniqueConstraint("name", "version", name="uq_skill_name_version"),
    )

    name: Mapped[str] = mapped_column(sa.String(128), index=True)
    description: Mapped[str] = mapped_column(sa.Text, default="")
    version: Mapped[int] = mapped_column(default=1)
    skill_type: Mapped[SkillType] = mapped_column(
        sa.Enum(SkillType, native_enum=False, length=16), default=SkillType.PROMPT)
    trigger_keywords: Mapped[list] = mapped_column(JSONType, default=list)
    files: Mapped[dict] = mapped_column(JSONType, default=dict)  # {"skill.yaml": "...", ...}
    entry: Mapped[str] = mapped_column(sa.String(255), default="")  # prompt.md / main.py
    status: Mapped[SkillStatus] = mapped_column(
        sa.Enum(SkillStatus, native_enum=False, length=16), default=SkillStatus.DISABLED)
    created_by: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id"), nullable=True)
    created_by_name: Mapped[str] = mapped_column(sa.String(128), default="")
    invoke_count: Mapped[int] = mapped_column(default=0)
    last_invoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
