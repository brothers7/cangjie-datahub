"""异步引擎与会话工厂（应用库）。"""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.core.config import settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.app_db_url,
            echo=False,
            pool_pre_ping=True,
            connect_args={"timeout": 15} if settings.is_sqlite_app_db else {},
        )
        if settings.is_sqlite_app_db:
            # SQLite 本地开发：开 WAL + 外键
            @event.listens_for(_engine.sync_engine, "connect")
            def _sqlite_pragma(dbapi_conn, _record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    return _engine


def get_session_factory() -> async_sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), expire_on_commit=False, autoflush=False
        )
    return _session_factory


async def get_db() -> AsyncIterator:
    """FastAPI 依赖。"""
    async with get_session_factory()() as session:
        yield session


def dispose_engine() -> None:
    global _engine, _session_factory
    _engine = None
    _session_factory = None
