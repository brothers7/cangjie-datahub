"""元数据：扫描得到的表 / 字段快照。"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, JSONType, TimestampMixin, UTCDateTime, UUIDMixin


class TableLayer(str, enum.Enum):
    ODS = "ods"
    DWD = "dwd"
    DWS = "dws"
    ADS = "ads"
    DIM = "dim"
    UNKNOWN = "unknown"


class SourceKind(str, enum.Enum):
    AUTO = "auto"      # 扫描自动生成
    MANUAL = "manual"  # 纯人工创建，扫描永不触碰
    HYBRID = "hybrid"  # 自动生成但人工修改过部分字段（edited_fields 保护）


class SemanticRole(str, enum.Enum):
    METRIC = "metric"
    DIMENSION = "dimension"
    TIME = "time"
    UNKNOWN = "unknown"


class AggFunc(str, enum.Enum):
    SUM = "sum"
    COUNT = "count"
    AVG = "avg"
    COUNT_DISTINCT = "count_distinct"
    MAX = "max"
    MIN = "min"


class MetaTable(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "meta_tables"
    __table_args__ = (
        sa.UniqueConstraint("datasource_id", "schema_name", "table_name", name="uq_ds_schema_table"),
    )

    datasource_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("datasources.id", ondelete="CASCADE"), index=True
    )
    schema_name: Mapped[str] = mapped_column(sa.String(128), default="")
    table_name: Mapped[str] = mapped_column(sa.String(255), index=True)
    comment: Mapped[str] = mapped_column(sa.Text, default="")
    layer: Mapped[TableLayer] = mapped_column(
        sa.Enum(TableLayer, native_enum=False, length=16), default=TableLayer.UNKNOWN
    )
    row_count: Mapped[int | None] = mapped_column(default=None)
    partition_cols: Mapped[list] = mapped_column(JSONType, default=list)
    meta_hash: Mapped[str] = mapped_column(sa.String(64), default="", index=True)
    source: Mapped[SourceKind] = mapped_column(
        sa.Enum(SourceKind, native_enum=False, length=16), default=SourceKind.AUTO
    )
    edited_fields: Mapped[list] = mapped_column(JSONType, default=list)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class MetaColumn(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "meta_columns"
    __table_args__ = (
        sa.UniqueConstraint("table_id", "column_name", name="uq_table_column"),
    )

    table_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("meta_tables.id", ondelete="CASCADE"), index=True
    )
    column_name: Mapped[str] = mapped_column(sa.String(255))
    data_type: Mapped[str] = mapped_column(sa.String(64), default="")
    comment: Mapped[str] = mapped_column(sa.Text, default="")
    is_primary_key: Mapped[bool] = mapped_column(default=False)
    is_nullable: Mapped[bool] = mapped_column(default=True)
    semantic_role: Mapped[SemanticRole] = mapped_column(
        sa.Enum(SemanticRole, native_enum=False, length=16), default=SemanticRole.UNKNOWN
    )
    agg_hint: Mapped[str | None] = mapped_column(sa.String(32), default=None)
    cardinality_ratio: Mapped[float | None] = mapped_column(default=None)  # 采样 distinct/total
    meta_hash: Mapped[str] = mapped_column(sa.String(64), default="")
    source: Mapped[SourceKind] = mapped_column(
        sa.Enum(SourceKind, native_enum=False, length=16), default=SourceKind.AUTO
    )
    edited_fields: Mapped[list] = mapped_column(JSONType, default=list)
    value_examples: Mapped[list] = mapped_column(JSONType, default=list)  # 维度枚举采样
