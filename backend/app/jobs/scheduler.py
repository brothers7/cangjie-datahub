"""定时任务：按数据源 scan_cron 表达式增量扫描 + 夜间反思 + 缓存清理。"""
from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings

logger = logging.getLogger("jobs")

_scheduler: AsyncIOScheduler | None = None
# 记录已调度的数据源 cron 任务，便于增删改后同步
_DS_JOB_PREFIX = "ds_scan:"


async def incremental_scan_datasource(datasource_id: str) -> None:
    """对单个数据源做增量扫描（meta_hash 差异跳过无变化表）。"""

    from app.db.models import DataSource, ScanJob, ScanJobStatus, ScanMode
    from app.db.models.base import utcnow
    from app.db.session import get_session_factory
    from app.metadata.scanner import scan_datasource

    async with get_session_factory()() as session:
        ds = await session.get(DataSource, datasource_id)
        if ds is None:
            return
        try:
            job = ScanJob(datasource_id=ds.id, mode=ScanMode.INCREMENTAL,
                          triggered_by=f"cron:{ds.scan_cron}", status=ScanJobStatus.RUNNING,
                          started_at=utcnow())
            session.add(job)
            await session.flush()
            await scan_datasource(session, ds, ScanMode.INCREMENTAL, job)
            await session.commit()
            logger.info("incremental scan done for %s", ds.name)
        except Exception:
            logger.exception("incremental scan failed for %s", ds.name)
            await session.rollback()


def _parse_cron(expr: str) -> CronTrigger | None:
    """解析 5 段 cron 表达式（分 时 日 月 周）。非法返回 None。"""
    parts = (expr or "").split()
    if len(parts) != 5:
        return None
    try:
        return CronTrigger(minute=parts[0], hour=parts[1], day=parts[2],
                           month=parts[3], day_of_week=parts[4])
    except Exception:
        logger.warning("invalid scan_cron: %r", expr)
        return None


def sync_datasource_jobs(datasources: list) -> None:
    """按数据源 scan_cron 重建调度任务；datasources 为 DataSource 列表。"""
    if _scheduler is None:
        return
    want: dict[str, CronTrigger] = {}
    for ds in datasources:
        if not ds.scan_cron:
            continue
        trig = _parse_cron(ds.scan_cron)
        if trig is not None:
            want[str(ds.id)] = trig
    # 移除已不存在/清空 cron 的任务
    for job in _scheduler.get_jobs():
        if job.id.startswith(_DS_JOB_PREFIX) and job.id[len(_DS_JOB_PREFIX):] not in want:
            _scheduler.remove_job(job.id)
    # 新增/替换
    for ds_id, trig in want.items():
        _scheduler.add_job(incremental_scan_datasource, trig,
                           args=[ds_id], id=_DS_JOB_PREFIX + ds_id,
                           replace_existing=True, max_instances=1, coalesce=True)


async def refresh_scan_jobs() -> None:
    """从 DB 读取所有数据源的 scan_cron 并同步到调度器（启动和每小时兜底）。"""
    from sqlalchemy import select

    from app.db.models import DataSource
    from app.db.session import get_session_factory

    async with get_session_factory()() as session:
        sources = (await session.execute(
            select(DataSource).where(DataSource.scan_cron.isnot(None)))).scalars().all()
    sync_datasource_jobs(list(sources))
    if sources:
        logger.info("scheduled %d datasource scan cron jobs", len(sources))


async def nightly_reflection() -> None:
    """夜间反思：总结活跃用户习惯 + 沉淀报表模板。"""
    from sqlalchemy import select

    from app.agent.memory import reflection
    from app.db.models import User, UserStatus
    from app.db.session import get_session_factory

    async with get_session_factory()() as session:
        users = (await session.execute(
            select(User).where(User.status == UserStatus.ACTIVE))).scalars().all()
        for u in users[:200]:
            try:
                await reflection.reflect_user(session, u.id, u.display_name)
            except Exception:
                logger.exception("reflect user %s failed", u.username)
        try:
            n = await reflection.sediment_report_templates(session)
            if n:
                logger.info("sedimented %d report templates", n)
        except Exception:
            logger.exception("sediment templates failed")
        await session.commit()


async def scheduler_tick_job() -> None:
    """内置调度引擎节拍（PRD 3.3G）：到期任务生成实例 → 依赖放行 → 双重闸门执行。"""
    from app.db.session import get_session_factory
    from app.services import scheduler_engine

    async with get_session_factory()() as session:
        try:
            stats = await scheduler_engine.tick(session)
            await session.commit()
            if stats["created"] or stats["ran"] or stats["blocked"]:
                logger.info("scheduler tick: %s", stats)
        except Exception:
            logger.exception("scheduler tick failed")
            await session.rollback()


async def scheduler_catchup_job() -> None:
    """停机补偿：启动/恢复时回补窗口内错过的 cron 时点（幂等，与 tick 共用去重键）。"""
    from app.db.session import get_session_factory
    from app.services import scheduler_engine

    async with get_session_factory()() as session:
        try:
            stats = await scheduler_engine.catchup(session)
            await session.commit()
            if stats.get("created"):
                logger.info("scheduler catchup: %s", stats)
        except Exception:
            logger.exception("scheduler catchup failed")
            await session.rollback()


async def purge_cache_job() -> None:
    from app.db.session import get_session_factory
    from app.services.cache_service import purge_expired

    async with get_session_factory()() as session:
        n = await purge_expired(session)
        await session.commit()
        if n:
            logger.info("purged %d cache entries", n)


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    # 调度时区可配置（config 并行迭代补齐，未就位回退 Asia/Shanghai）
    tz = getattr(settings, "app_tz", "Asia/Shanghai") or "Asia/Shanghai"
    _scheduler = AsyncIOScheduler(timezone=tz)
    # 启动时载入各数据源 cron；之后数据源增删改时由 API 层调 refresh_scan_jobs
    _scheduler.add_job(refresh_scan_jobs, CronTrigger(minute=0), id="refresh_scan_jobs",
                       max_instances=1, coalesce=True)
    _scheduler.add_job(nightly_reflection, CronTrigger(hour=2, minute=30), id="reflection",
                       max_instances=1, coalesce=True)
    _scheduler.add_job(purge_cache_job, CronTrigger(minute="*/10"), id="purge_cache",
                       max_instances=1, coalesce=True)
    _scheduler.add_job(scheduler_tick_job, CronTrigger(second=5), id="scheduler_tick",
                       max_instances=1, coalesce=True)
    _scheduler.start()
    asyncio.get_event_loop().create_task(refresh_scan_jobs())
    # 启动/恢复时做一次停机补偿（回补窗口内错过的 cron 时点）
    asyncio.get_event_loop().create_task(scheduler_catchup_job())
    logger.info("scheduler started")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        # wait=True：优雅停机，有界等待在途任务收尾（避免进程退出时实例停在
        # running 半态）；等待上限由各任务自身超时（query_timeout 等）兜底
        _scheduler.shutdown(wait=True)
        _scheduler = None
