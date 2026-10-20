"""核心实体的请求/响应 DTO。"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ---------- 认证 / 用户 ----------
class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    token: str
    username: str
    display_name: str
    role: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    username: str
    display_name: str
    role: str
    status: str
    last_login_at: datetime | None = None
    created_at: datetime


class UserCreateIn(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    display_name: str = ""
    role: str = "viewer"


class UserUpdateIn(BaseModel):
    display_name: str | None = None
    role: str | None = None
    status: str | None = None
    password: str | None = None


# ---------- 数据源 ----------
class DatasourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    db_type: str
    host: str = ""
    port: int = 0
    database_name: str = ""
    username: str = ""
    password: str = ""          # 仅写；更新时留空表示不修改
    extra_params: dict = {}
    table_whitelist: list[str] = []
    table_blacklist: list[str] = []
    scan_cron: str | None = None
    sched_write_enabled: bool = False   # 允许内置调度执行加工 SQL（PRD 3.3G，默认关）
    readonly_enforced: bool = True      # 强制只读会话（PRD 4.4，默认开；特殊引擎可关）


class DatasourceUpdateIn(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = None
    database_name: str | None = None
    username: str | None = None
    password: str | None = None
    extra_params: dict | None = None
    table_whitelist: list[str] | None = None
    table_blacklist: list[str] | None = None
    scan_cron: str | None = None
    sched_write_enabled: bool | None = None
    readonly_enforced: bool | None = None


class DatasourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    db_type: str
    host: str
    port: int
    database_name: str
    username: str
    status: str
    status_message: str
    table_whitelist: list
    table_blacklist: list
    scan_cron: str | None
    sched_write_enabled: bool
    readonly_enforced: bool
    is_builtin: bool
    last_test_at: datetime | None
    last_scan_at: datetime | None
    created_at: datetime


# ---------- LLM 配置 ----------
class LlmConfigIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    protocol: str                  # openai / anthropic
    kind: str = "chat"             # chat / embedding
    endpoint: str
    api_key: str = ""              # 仅写
    model: str
    temperature: float = 0.2
    max_tokens: int = 4096
    top_p: float = 1.0
    timeout_sec: int = 30
    retry_count: int = 2
    stream_enabled: bool = True
    is_default: bool = False
    is_default_embedding: bool = False
    fallback_config_id: str | None = None
    switch_policy: str = "immediate"


class LlmConfigUpdateIn(BaseModel):
    name: str | None = None
    endpoint: str | None = None
    api_key: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    timeout_sec: int | None = None
    retry_count: int | None = None
    stream_enabled: bool | None = None
    is_default: bool | None = None
    is_default_embedding: bool | None = None
    fallback_config_id: str | None = None
    switch_policy: str | None = None


class LlmConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    protocol: str
    kind: str
    endpoint: str
    model: str
    temperature: float
    max_tokens: int
    top_p: float
    timeout_sec: int
    retry_count: int
    stream_enabled: bool
    is_default: bool
    is_default_embedding: bool
    fallback_config_id: uuid.UUID | None
    switch_policy: str
    status: str
    status_message: str
    health_latency_ms: int | None
    # 近 15 分钟调用失败率（列表端点聚合附加；无调用为 None/0）
    failure_rate_15m: float | None = None
    calls_15m: int | None = None
    last_health_at: datetime | None


# ---------- 知识图谱 ----------
class MetricUpdateIn(BaseModel):
    name: str | None = None
    aliases: list[str] | None = None
    business_def: str | None = None
    formula: str | None = None
    agg_function: str | None = None
    unit: str | None = None
    value_range: list[float] | None = None
    period_grain: str | None = None
    business_domain: str | None = None
    status: str | None = None


class MetricCreateIn(BaseModel):
    name: str
    aliases: list[str] = []
    business_def: str = ""
    formula: str = ""
    agg_function: str | None = None
    unit: str = ""
    value_range: list[float] | None = None
    business_domain: str = "general"
    table_id: str | None = None
    column_id: str | None = None


class DimensionUpdateIn(BaseModel):
    name: str | None = None
    aliases: list[str] | None = None
    description: str | None = None
    dimension_type: str | None = None
    value_examples: list | None = None
    status: str | None = None


class RuleIn(BaseModel):
    rule_type: str
    target_type: str = "global"
    target_id: str | None = None
    rule_config: dict = {}
    severity: str = "block"
    message: str = ""
    enabled: bool = True


class MetricMergeIn(BaseModel):
    target_metric_id: str


# ---------- 对话 ----------
class ConversationCreateIn(BaseModel):
    title: str = ""
    datasource_id: str | None = None
    llm_config_id: str | None = None   # 会话级模型覆盖


class MessageIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    confirm: dict | None = None   # {metric_id: ...} 低置信确认后续跑
