"""Alembic 异步迁移环境。

- metadata 直接复用 app.db.models.Base.metadata（单一事实来源，模型即迁移目标）
- 数据库 URL 优先取 alembic.ini 的 sqlalchemy.url，缺省回落到应用配置
  settings.app_db_url（APP_DB_URL 环境变量），与应用运行库保持一致
- 老部署（库已由旧版 create_all 建表）首次接入：
  `alembic -c alembic/alembic.ini stamp head`，后续 upgrade 只跑增量
"""
from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

# backend/ 加入 sys.path，保证可 import app.*（ini 里 prepend_sys_path=. 指向的是 ini 所在目录）
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.core.config import settings  # noqa: E402
from app.db.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    # 不禁用既有 logger（测试/嵌入场景下避免静默掉应用日志）
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _db_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    return url or settings.app_db_url


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。"""
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """在线模式：异步引擎连库执行。"""
    engine = create_async_engine(_db_url())
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
