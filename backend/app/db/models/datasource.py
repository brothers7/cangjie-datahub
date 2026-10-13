"""数据源连接配置。"""
from __future__ import annotations

import enum
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, JSONType, TimestampMixin, UTCDateTime, UUIDMixin


class DbType(str, enum.Enum):
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"
    CLICKHOUSE = "clickhouse"
    SQLITE = "sqlite"
    # 预留（第二期适配）
    HIVE = "hive"
    SPARK = "spark"
    DORIS = "doris"
    STARROCKS = "starrocks"


class DsStatus(str, enum.Enum):
    CONNECTED = "connected"
    ERROR = "error"
    UNTESTED = "untested"


class DataSource(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "datasources"

    name: Mapped[str] = mapped_column(sa.String(128), unique=True)
    db_type: Mapped[DbType] = mapped_column(sa.Enum(DbType, native_enum=False, length=16))
    host: Mapped[str] = mapped_column(sa.String(255), default="")
    port: Mapped[int] = mapped_column(default=0)
    database_name: Mapped[str] = mapped_column(sa.String(255), default="")
    username: Mapped[str] = mapped_column(sa.String(128), default="")
    password_cipher: Mapped[str] = mapped_column(sa.Text, default="")  # AES-GCM 加密
    extra_params: Mapped[dict] = mapped_column(JSONType, default=dict)
    # 文件型库（SQLite）路径也存 extra_params["path"]
    table_whitelist: Mapped[list] = mapped_column(JSONType, default=list)
    table_blacklist: Mapped[list] = mapped_column(JSONType, default=list)
    readonly_enforced: Mapped[bool] = mapped_column(default=True)
    sched_write_enabled: Mapped[bool] = mapped_column(default=False)  # 允许内置调度执行加工 SQL（PRD 3.3G，默认关）
    is_builtin: Mapped[bool] = mapped_column(default=False)  # 内置演示库，不可删除
    status: Mapped[DsStatus] = mapped_column(
        sa.Enum(DsStatus, native_enum=False, length=16), default=DsStatus.UNTESTED
    )
    status_message: Mapped[str] = mapped_column(sa.Text, default="")
    last_test_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_scan_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    scan_cron: Mapped[str | None] = mapped_column(sa.String(64))  # 如 "0 */6 * * *"

    @property
    def target(self) -> str:
        return f"{self.db_type.value}://{self.host}:{self.port}/{self.database_name}" if self.db_type != DbType.SQLITE \
            else f"sqlite://{self.database_name or self.extra_params.get('path', '')}"
