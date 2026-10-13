"""知识图谱：指标 / 维度 / 边 / 约束规则。"""
from __future__ import annotations

import enum
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, JSONType, TimestampMixin, UUIDMixin, UUIDType
from app.db.models.metadata import AggFunc, SourceKind


class MetricStatus(str, enum.Enum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    MERGED = "merged"


class DimensionType(str, enum.Enum):
    TIME = "time"
    GEO = "geo"
    CHANNEL = "channel"
    PRODUCT = "product"
    USER_SEGMENT = "user_segment"
    DEPT = "dept"
    OTHER = "other"


class KgMetric(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "kg_metrics"

    name: Mapped[str] = mapped_column(sa.String(255), unique=True, index=True)
    aliases: Mapped[list] = mapped_column(JSONType, default=list)
    business_def: Mapped[str] = mapped_column(sa.Text, default="")  # 业务口径
    formula: Mapped[str] = mapped_column(sa.Text, default="")       # 计算公式
    agg_function: Mapped[AggFunc | None] = mapped_column(sa.String(32), default=None)
    unit: Mapped[str] = mapped_column(sa.String(32), default="")    # 元/单/%
    value_range: Mapped[list | None] = mapped_column(JSONType, default=None)  # [min, max]
    period_grain: Mapped[str] = mapped_column(sa.String(16), default="day")
    business_domain: Mapped[str] = mapped_column(sa.String(64), default="general")
    table_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("meta_tables.id", ondelete="SET NULL"), index=True
    )
    column_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("meta_columns.id", ondelete="SET NULL")
    )
    parent_metric_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("kg_metrics.id"))
    status: Mapped[MetricStatus] = mapped_column(
        sa.Enum(MetricStatus, native_enum=False, length=16), default=MetricStatus.ACTIVE
    )
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("kg_metrics.id"))
    confidence: Mapped[float] = mapped_column(default=0.0)
    source: Mapped[SourceKind] = mapped_column(
        sa.Enum(SourceKind, native_enum=False, length=16), default=SourceKind.AUTO
    )
    edited_fields: Mapped[list] = mapped_column(JSONType, default=list)
    lock_version: Mapped[int] = mapped_column(default=0)


class KgDimension(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "kg_dimensions"

    name: Mapped[str] = mapped_column(sa.String(255), unique=True, index=True)
    aliases: Mapped[list] = mapped_column(JSONType, default=list)
    description: Mapped[str] = mapped_column(sa.Text, default="")
    dimension_type: Mapped[DimensionType] = mapped_column(
        sa.Enum(DimensionType, native_enum=False, length=16), default=DimensionType.OTHER
    )
    value_examples: Mapped[list] = mapped_column(JSONType, default=list)
    related_columns: Mapped[list] = mapped_column(JSONType, default=list)  # [{table_id, column_id}]
    status: Mapped[MetricStatus] = mapped_column(
        sa.Enum(MetricStatus, native_enum=False, length=16), default=MetricStatus.ACTIVE
    )
    source: Mapped[SourceKind] = mapped_column(
        sa.Enum(SourceKind, native_enum=False, length=16), default=SourceKind.AUTO
    )
    edited_fields: Mapped[list] = mapped_column(JSONType, default=list)


class EdgeType(str, enum.Enum):
    METRIC_ON_TABLE = "METRIC_ON_TABLE"
    DIM_ON_TABLE = "DIM_ON_TABLE"
    METRIC_DIM_ALLOWED = "METRIC_DIM_ALLOWED"
    METRIC_DIM_FORBIDDEN = "METRIC_DIM_FORBIDDEN"
    METRIC_PARENT = "METRIC_PARENT"
    TABLE_JOIN = "TABLE_JOIN"


class EntityType(str, enum.Enum):
    METRIC = "metric"
    DIMENSION = "dimension"
    TABLE = "table"


class KgEdge(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "kg_edges"
    __table_args__ = (
        sa.UniqueConstraint(
            "edge_type", "src_type", "src_id", "dst_type", "dst_id", name="uq_edge"
        ),
    )

    src_type: Mapped[EntityType] = mapped_column(sa.Enum(EntityType, native_enum=False, length=16))
    src_id: Mapped[uuid.UUID] = mapped_column(UUIDType(), index=True)
    dst_type: Mapped[EntityType] = mapped_column(sa.Enum(EntityType, native_enum=False, length=16))
    dst_id: Mapped[uuid.UUID] = mapped_column(UUIDType(), index=True)
    edge_type: Mapped[EdgeType] = mapped_column(sa.Enum(EdgeType, native_enum=False, length=32), index=True)
    properties: Mapped[dict] = mapped_column(JSONType, default=dict)  # join_keys/join_type 等
    source: Mapped[SourceKind] = mapped_column(
        sa.Enum(SourceKind, native_enum=False, length=16), default=SourceKind.AUTO
    )


class RuleType(str, enum.Enum):
    FORBIDDEN_DIM_COMBO = "forbidden_dim_combo"
    METRIC_VALUE_RANGE = "metric_value_range"
    METRIC_AGG_REQUIRED = "metric_agg_required"
    METRIC_DEF_REQUIRED = "metric_def_required"


class RuleSeverity(str, enum.Enum):
    BLOCK = "block"
    WARN = "warn"


class KgValidationRule(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "kg_validation_rules"

    rule_type: Mapped[RuleType] = mapped_column(sa.Enum(RuleType, native_enum=False, length=32))
    target_type: Mapped[str] = mapped_column(sa.String(16), default="global")  # metric/global
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType(), default=None)
    rule_config: Mapped[dict] = mapped_column(JSONType, default=dict)
    severity: Mapped[RuleSeverity] = mapped_column(
        sa.Enum(RuleSeverity, native_enum=False, length=8), default=RuleSeverity.BLOCK
    )
    message: Mapped[str] = mapped_column(sa.String(255), default="")
    enabled: Mapped[bool] = mapped_column(default=True)
    source: Mapped[SourceKind] = mapped_column(
        sa.Enum(SourceKind, native_enum=False, length=16), default=SourceKind.AUTO
    )
