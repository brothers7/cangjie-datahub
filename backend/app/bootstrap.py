"""首启引导：建表 / 默认管理员 / 演示库 / 数据源注册 / 首次全量扫描建图谱。

幂等；Docker entrypoint 与本地 SQLite 模式都会调用。
CLI: python -m app.bootstrap [--reseed-demo]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import uuid

from sqlalchemy import select

from app.core.config import settings
from app.core.security import hash_password
from app.db.models import Base, DataSource, DsStatus, DbType, Role, ScanJob, ScanJobStatus, ScanMode, User
from app.db.models.base import utcnow
from app.db.session import dispose_engine, get_engine, get_session_factory
from app.kg.validator import ensure_default_rules

logger = logging.getLogger("bootstrap")

DEMO_DS_NAME = "演示电商库(SQLite)"


async def _migrate_columns() -> None:
    """轻量列补齐：对已存在的表补新增列（create_all 不会 ALTER）。
    只加可空列，幂等；不支持的方言静默跳过。"""
    import sqlalchemy as sa

    additions = [
        ("conversations", "llm_config_id", "VARCHAR(36)"),
        ("etl_tasks", "cron", "VARCHAR(64) DEFAULT ''"),
        ("etl_tasks", "depends_on", "TEXT DEFAULT '[]'"),
        ("etl_tasks", "source", "VARCHAR(16) DEFAULT 'manual'"),
        ("etl_tasks", "modeling_session_id", "VARCHAR(36)"),
        ("etl_tasks", "sched_enabled", "BOOLEAN DEFAULT 0"),
        ("etl_tasks", "approved_by", "VARCHAR(64) DEFAULT ''"),
        ("etl_tasks", "approved_at", "DATETIME"),
        ("datasources", "sched_write_enabled", "BOOLEAN DEFAULT 0"),
        ("modeling_sessions", "llm_config_id", "VARCHAR(36)"),
        ("scheduler_runs", "slot", "VARCHAR(8) DEFAULT ''"),
        ("users", "failed_login_count", "INTEGER DEFAULT 0"),
        ("users", "locked_until", "DATETIME"),
        ("azkaban_configs", "verify_tls", "BOOLEAN DEFAULT 0"),
        ("airflow_configs", "verify_tls", "BOOLEAN DEFAULT 0"),
    ]
    async with get_engine().begin() as conn:
        for table, column, ddl in additions:
            try:
                rows = await conn.execute(
                    sa.text(
                        "SELECT name FROM pragma_table_info(:t)" if _is_sqlite() else
                        "SELECT column_name AS name FROM information_schema.columns "
                        "WHERE table_name=:t"
                    ), {"t": table})
                cols = {r[0] for r in rows}
                if cols and column not in cols:
                    await conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                    logger.info("migrated: %s.%s added", table, column)
            except Exception as e:
                logger.debug("column migrate skip %s.%s: %s", table, column, e)


def _is_sqlite() -> bool:
    return settings.app_db_url.startswith("sqlite")


async def bootstrap(reseed_demo: bool = False) -> None:
    # 1. 建表（幂等）+ 存量表补列
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _migrate_columns()
    logger.info("tables ready")

    async with get_session_factory()() as session:
        # 2. 默认管理员
        admin = (await session.execute(
            select(User).where(User.role == Role.ADMIN))).scalars().first()
        if admin is None:
            session.add(User(username=settings.default_admin_username,
                             password_hash=hash_password(settings.default_admin_password),
                             display_name="系统管理员", role=Role.ADMIN))
            await session.commit()
            logger.info("default admin created: %s", settings.default_admin_username)

        # 3. 演示库 + 数据源注册 + 首次全量扫描
        if settings.demo_db_enabled:
            demo_ds = (await session.execute(
                select(DataSource).where(DataSource.name == DEMO_DS_NAME))).scalar_one_or_none()
            need_scan = demo_ds is None
            if demo_ds is None:
                from app.core.crypto import encrypt
                demo_ds = DataSource(
                    name=DEMO_DS_NAME, db_type=DbType.SQLITE,
                    database_name=settings.demo_db_path,
                    extra_params={"path": settings.demo_db_path},
                    is_builtin=True, status=DsStatus.UNTESTED)
                session.add(demo_ds)
                await session.flush()
                demo_ds.password_cipher = encrypt("", aad=f"ds:{demo_ds.id}")
                await session.commit()
            from app.demo.seed import build_demo_db
            import pathlib
            exists = pathlib.Path(settings.demo_db_path).exists()
            if (reseed_demo or not exists):
                build_demo_db(settings.demo_db_path, force=True)
                need_scan = True
                logger.info("demo db seeded at %s", settings.demo_db_path)
            if need_scan:
                job = ScanJob(datasource_id=demo_ds.id, mode=ScanMode.FULL,
                              triggered_by="bootstrap", status=ScanJobStatus.RUNNING,
                              started_at=utcnow())
                session.add(job)
                await session.flush()
                from app.metadata.scanner import scan_datasource
                report = await scan_datasource(session, demo_ds, ScanMode.FULL, job)
                demo_ds.last_scan_at = utcnow()
                demo_ds.status = DsStatus.CONNECTED
                await session.commit()
                logger.info("demo datasource scanned: %s tables, entities=%s",
                            report["tables_total"], report.get("entities"))

        # 4. 默认图谱规则
        await ensure_default_rules(session)
        await session.commit()

    # 5. 指标文本向量索引（embedding 可用时）
    try:
        from app.agent.memory import long_term
        async with get_session_factory()() as session:
            from app.db.models import KgMetric, MetricStatus
            metrics = (await session.execute(
                select(KgMetric).where(KgMetric.status == MetricStatus.ACTIVE))).scalars().all()
            if metrics and await long_term.embedding_available():
                n = await long_term.index_metric_texts(metrics)
                logger.info("indexed %d metric texts into qdrant", n)
    except Exception as e:
        logger.debug("metric vector index skipped: %s", e)

    dispose_engine()
    logger.info("bootstrap done")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--reseed-demo", action="store_true", help="重建演示库并重扫图谱")
    args = ap.parse_args()
    asyncio.run(bootstrap(reseed_demo=args.reseed_demo))


if __name__ == "__main__":
    main()
