"""首启引导：建表 / 默认管理员 / 演示库 / 数据源注册 / 首次全量扫描建图谱。

幂等；Docker entrypoint 与本地 SQLite 模式都会调用。
CLI: python -m app.bootstrap [--reseed-demo]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select

from app.core.config import settings
from app.core.security import hash_password
from app.db.models import Base, DataSource, DsStatus, DbType, Role, ScanJob, ScanJobStatus, ScanMode, User
from app.db.models.base import utcnow
from app.db.session import get_engine, get_session_factory
from app.kg.validator import ensure_default_rules

logger = logging.getLogger("bootstrap")

DEMO_DS_NAME = "演示电商库(SQLite)"

# 悬挂扫描任务阈值：RUNNING 且 started_at 超过该时长视为进程崩溃遗留（可在 settings 配置）
STALE_SCAN_JOB_MINUTES = settings.stale_scan_job_minutes


async def reap_stale_scan_jobs(session) -> int:
    """回收悬挂扫描任务：把 RUNNING 且 started_at 超过阈值的 ScanJob 标记 FAILED。

    进程崩溃/重启会遗留永远 RUNNING 的扫描任务（既误导用户，也会阻塞手动触发
    的互斥检查）。应用启动时调用一次。
    """
    stale_minutes = settings.stale_scan_job_minutes
    threshold = utcnow() - timedelta(minutes=stale_minutes)
    rows = (await session.execute(select(ScanJob).where(
        ScanJob.status == ScanJobStatus.RUNNING,
        ScanJob.started_at.isnot(None),
        ScanJob.started_at < threshold))).scalars().all()
    for j in rows:
        j.status = ScanJobStatus.FAILED
        j.finished_at = utcnow()
        j.error_log = (j.error_log or []) + [
            f"进程重启回收：任务 RUNNING 超过 {stale_minutes} 分钟，标记失败"]
    if rows:
        logger.info("reaped %d stale scan jobs", len(rows))
    return len(rows)


async def dispose_app_engine() -> None:
    """真正关闭应用库引擎连接池。

    db.session.dispose_engine() 只把全局引用置 None，底层 AsyncEngine 的连接池
    不会释放；先 await engine.dispose() 再清引用，保证后续 get_engine() 重建的
    是全新引擎，不残留双引擎。
    """
    import app.db.session as db_session
    eng = db_session._engine
    if eng is not None:
        await eng.dispose()
    db_session.dispose_engine()


def _column_additions(dialect_name: str) -> list[tuple[str, str, str]]:
    """存量表待补列清单（table, column, DDL）。

    BOOLEAN 默认值按方言生成：PostgreSQL 只接受 true/false 字面量（写 0 会报错，
    此前静默跳过导致 PG 部署缺列），SQLite/MySQL 用 0。
    """
    bool_false = "false" if dialect_name == "postgresql" else "0"
    return [
        ("conversations", "llm_config_id", "VARCHAR(36)"),
        ("etl_tasks", "cron", "VARCHAR(64) DEFAULT ''"),
        ("etl_tasks", "depends_on", "TEXT DEFAULT '[]'"),
        ("etl_tasks", "source", "VARCHAR(16) DEFAULT 'manual'"),
        ("etl_tasks", "modeling_session_id", "VARCHAR(36)"),
        ("etl_tasks", "sched_enabled", f"BOOLEAN DEFAULT {bool_false}"),
        ("etl_tasks", "approved_by", "VARCHAR(64) DEFAULT ''"),
        ("etl_tasks", "approved_at", "DATETIME"),
        ("datasources", "sched_write_enabled", f"BOOLEAN DEFAULT {bool_false}"),
        ("modeling_sessions", "llm_config_id", "VARCHAR(36)"),
        ("scheduler_runs", "slot", "VARCHAR(8) DEFAULT ''"),
        ("users", "failed_login_count", "INTEGER DEFAULT 0"),
        ("users", "locked_until", "DATETIME"),
        ("azkaban_configs", "verify_tls", f"BOOLEAN DEFAULT {bool_false}"),
        ("airflow_configs", "verify_tls", f"BOOLEAN DEFAULT {bool_false}"),
        ("meta_tables", "create_time", "DATETIME"),
        ("meta_tables", "storage_format", "VARCHAR(64)"),
        ("conversations", "llm_snapshot_config_id", "VARCHAR(36)"),
    ]


async def _migrate_columns() -> None:
    """轻量列补齐：对已存在的表补新增列（create_all 不会 ALTER）。

    过渡期兼容老部署的兜底手段，正式迁移请走 alembic（backend/alembic/）。
    只加可空列，幂等；单列失败不阻断其余列，结束后统一 warning 输出失败清单。
    """
    import sqlalchemy as sa

    failed: list[str] = []
    async with get_engine().begin() as conn:
        dialect_name = conn.dialect.name
        for table, column, ddl in _column_additions(dialect_name):
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
                failed.append(f"{table}.{column}")
                logger.warning("column migrate failed %s.%s: %s", table, column, e)
    if failed:
        logger.warning("column migrate 失败列清单（需人工处理或改用 alembic 迁移）: %s",
                       ", ".join(failed))


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

    # 6. 回收悬挂扫描任务（上次进程崩溃遗留的 RUNNING 任务）
    try:
        async with get_session_factory()() as session:
            await reap_stale_scan_jobs(session)
            await session.commit()
    except Exception as e:
        logger.debug("reap stale scan jobs skipped: %s", e)

    # 7. 释放引导期引擎（真正关闭连接池，不只清全局引用）
    await dispose_app_engine()
    logger.info("bootstrap done")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--reseed-demo", action="store_true", help="重建演示库并重扫图谱")
    args = ap.parse_args()
    asyncio.run(bootstrap(reseed_demo=args.reseed_demo))


if __name__ == "__main__":
    main()
