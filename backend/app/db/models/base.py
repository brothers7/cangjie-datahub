"""ORM 基类与公共 Mixin。"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(dt: datetime | None) -> datetime | None:
    """统一时间基准：naive 一律视为 UTC，补齐 tzinfo。"""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class UTCDateTime(TypeDecorator):
    """带时区的时间列，跨方言统一为 aware UTC。

    SQLite 不带时区，读回 naive；与 utcnow()（aware）比较会抛 TypeError，
    序列化到前端也会丢时区导致显示偏移。此类型在读写两端统一补齐 UTC。
    """

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return as_utc(value)

    def process_result_value(self, value, dialect):
        return as_utc(value)


class UUIDType(TypeDecorator):
    """UUID 列：写入端容忍 str。

    管线上下文里的 id 大多是 str（来自 SSE 事件、请求体、图谱查询参数），
    直接把 str 交给 sa.Uuid() 会在 bind 阶段抛
    `AttributeError: 'str' object has no attribute 'hex'`。
    这里在写库前统一转成 uuid.UUID，读回仍是 uuid.UUID。
    非法字符串不静默转 None，原样下传让数据库报错，避免掩盖调用方的 bug。
    """

    impl = sa.Uuid()
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or isinstance(value, uuid.UUID):
            return value
        if isinstance(value, str):
            try:
                return uuid.UUID(value)
            except ValueError:
                return value
        return value

    def process_result_value(self, value, dialect):
        if value is None or isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


# JSONB(PG) / JSON(SQLite) 双兼容
JSONType = sa.JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    type_annotation_map = {
        dict: JSONType,
        list: JSONType,
    }


class UUIDMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), primary_key=True, default=new_uuid, index=True
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False
    )
