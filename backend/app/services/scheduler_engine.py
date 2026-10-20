"""内置调度引擎（PRD 3.3G）：cron 到期 → 依赖放行 → 双重闸门 → 执行加工 SQL。

安全红线：
- DDL 系统绝不自动在数仓执行——引擎只执行加工 SQL；
- 加工 SQL 自动执行需双重闸门：任务经管理员审核通过 + 数据源开启「允许内置调度写入」；
- 任一闸门未满足 → 实例「阻塞」，无任何写动作；
- 执行前仍做写语句安全校验（仅 INSERT 类，禁 DDL/DROP/TRUNCATE/UPDATE/DELETE）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import sqlalchemy as sa
import sqlglot
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.registry import get_connector
from app.core.config import settings
from app.db.models import (AuditLog, DataSource, EtlTask, SchedulerRun, SchedulerRunStatus)
from app.db.models.base import utcnow

logger = logging.getLogger("scheduler_engine")

DEP_TIMEOUT_HOURS = 24
# 单任务单次回补实例硬上限（窗口已由 scheduler_catchup_hours 限制，此为双保险）
CATCHUP_MAX_PER_TASK = 500


def normalize_cron(expr: str) -> str | None:
    """归一化为 5 段 cron（分 时 日 月 周）；兼容 6 段 Azkaban 风格（秒 分 时 日 月 周，? → *）。"""
    parts = (expr or "").replace("?", "*").split()
    if len(parts) == 6:
        parts = parts[1:]
    if len(parts) != 5:
        return None
    return " ".join(parts)


def _cron_trigger(expr: str) -> CronTrigger | None:
    """由 5/6 段 cron 表达式构造 CronTrigger，非法返回 None。"""
    norm = normalize_cron(expr)
    if norm is None:
        return None
    parts = norm.split()
    try:
        return CronTrigger(minute=parts[0], hour=parts[1], day=parts[2],
                           month=parts[3], day_of_week=parts[4])
    except Exception:
        return None


def cron_fire_slot(expr: str, now: datetime, window_sec: int = 130) -> str | None:
    """cron 在最近 window 内的触发时刻（HH:MM），未到期返回 None。

    窗口放宽到 130s：容忍一次 tick 延迟（错过 1 分钟节拍仍能在下一拍补建实例，
    去重由 (task_id, ds, slot) 保证不重复）。
    """
    trig = _cron_trigger(expr)
    if trig is None:
        return None
    prev = trig.get_next_fire_time(None, now - timedelta(seconds=window_sec + 1))
    if prev is None:
        return None
    prev = prev.replace(tzinfo=None, second=0, microsecond=0)
    if prev > now:
        return None
    return prev.strftime("%H:%M")


def cron_due(expr: str, now: datetime) -> bool:
    return cron_fire_slot(expr, now) is not None


def app_tz_name() -> str:
    """调度时区配置（config 由并行迭代补齐，未就位时回退 Asia/Shanghai）。"""
    return getattr(settings, "app_tz", "Asia/Shanghai") or "Asia/Shanghai"


def local_now() -> datetime:
    """调度口径统一为配置时区（settings.app_tz，默认 Asia/Shanghai）本地时间（naive）。"""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(app_tz_name())).replace(tzinfo=None)


def business_day(now: datetime) -> str:
    """业务日期 ds = 触发日前一日（T+1 加工惯例）。"""
    return (now - timedelta(days=1)).strftime("%Y-%m-%d")


def split_statements(sql: str) -> list[str]:
    """引号/注释感知的多语句切分（字面量里的分号不切）。"""
    stmts, buf, quote, i = [], [], None, 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
        elif ch == ";":
            part = "".join(buf).strip()
            if part:
                stmts.append(part)
            buf = []
        else:
            buf.append(ch)
        i += 1
    part = "".join(buf).strip()
    if part:
        stmts.append(part)
    return stmts


def validate_write_sql(sql: str) -> str | None:
    """加工 SQL 写安全校验：仅 INSERT 类语句，禁 DDL/危险写。返回错误信息或 None。"""
    if not sql.strip():
        return "加工 SQL 为空"
    try:
        stmts = [s for s in sqlglot.parse(sql) if s is not None]
    except Exception as e:
        return f"SQL 解析失败：{e}"
    if not stmts:
        return "SQL 为空"
    for st in stmts:
        if not isinstance(st, sqlglot.exp.Insert):
            return f"内置调度仅允许 INSERT 类加工语句，检出 {st.key.upper()}（DDL 请人工执行）"
    return None


def substitute_ds(sql: str, ds: str) -> str:
    return sql.replace("${ds}", ds).replace("${DS}", ds)


async def _dep_satisfied(db: AsyncSession, task: EtlTask, ds: str) -> tuple[bool, str]:
    """同 ds 的依赖任务实例全部成功才放行。"""
    dep_ids = [str(d) for d in (task.depends_on or []) if d]
    if not dep_ids:
        return True, ""
    tasks = (await db.execute(select(EtlTask).where(EtlTask.id.in_(dep_ids)))).scalars().all()
    found = {str(t.id): t for t in tasks}
    missing = [d for d in dep_ids if d not in found]
    if missing:
        return False, f"依赖任务已不存在（{len(missing)} 个），请调整 depends_on"
    runs = (await db.execute(select(SchedulerRun).where(
        SchedulerRun.task_id.in_(dep_ids), SchedulerRun.ds == ds))).scalars().all()
    ok = {str(r.task_id) for r in runs if r.status == SchedulerRunStatus.SUCCESS}
    pending = [found[d].name or found[d].target_table for d in dep_ids if d not in ok]
    if pending:
        return False, "等待依赖：" + "、".join(pending[:5])
    return True, ""


async def _execute_run(db: AsyncSession, run: SchedulerRun, task: EtlTask) -> None:
    """双重闸门 + 写校验后执行加工 SQL。"""
    if not task.approved_by:
        run.status = SchedulerRunStatus.BLOCKED
        run.message = "任务未审核通过（需管理员在任务详情点击「审核通过」）"
        return
    if not task.datasource_id:
        run.status = SchedulerRunStatus.BLOCKED
        run.message = "任务未绑定数据源，无法执行加工 SQL"
        return
    ds = await db.get(DataSource, task.datasource_id)
    if ds is None:
        run.status = SchedulerRunStatus.BLOCKED
        run.message = "数据源已不存在"
        return
    if not ds.sched_write_enabled:
        run.status = SchedulerRunStatus.BLOCKED
        run.message = f"数据源「{ds.name}」未开放内置调度写入（管理员在数据源配置中开启）"
        return
    err = validate_write_sql(task.etl_sql)
    if err:
        run.status = SchedulerRunStatus.BLOCKED
        run.message = err
        return

    run.status = SchedulerRunStatus.RUNNING
    run.started_at = utcnow()
    await db.flush()
    sql = substitute_ds(task.etl_sql, run.ds)
    try:
        conn = get_connector(ds)
        # 引擎直连执行（绕过只读 execute）：仅在双重闸门后到达此处
        def _run() -> None:
            eng = conn.engine()
            # 单语句（常见情形，无分号）直接执行，跳过引号/注释感知的切分扫描；
            # 多语句无法跨方言安全合并为一次 execute（且需保留逐条报错/顺序语义），
            # 故仍逐条执行，但共用同一事务（eng.begin 一次性提交）
            stmts = [sql] if ";" not in sql else split_statements(sql)
            with eng.begin() as c:
                for stmt in stmts:
                    c.execute(sa.text(stmt))
        await asyncio.to_thread(_run)
        run.status = SchedulerRunStatus.SUCCESS
        run.message = ""
    except Exception as e:
        run.status = SchedulerRunStatus.FAILED
        run.message = f"{e.__class__.__name__}: {str(e)[:300]}"
        logger.warning("scheduler run failed task=%s ds=%s: %s", task.id, run.ds, e)
    finally:
        run.finished_at = utcnow()
        if run.started_at:
            run.duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)


async def _process_waiting(db: AsyncSession, by_id: dict[str, EtlTask]) -> dict:
    """等待中的实例：依赖检查 → 放行执行 / 超时标记（tick 与 catchup 共用）。"""
    stats = {"ran": 0, "blocked": 0, "waiting": 0, "dep_timeout": 0}
    waiting = (await db.execute(select(SchedulerRun).where(
        SchedulerRun.status == SchedulerRunStatus.WAITING))).scalars().all()
    for run in waiting:
        task = by_id.get(str(run.task_id)) or await db.get(EtlTask, run.task_id)
        if task is None:
            run.status = SchedulerRunStatus.FAILED
            run.message = "任务已删除"
            continue
        if utcnow() - run.created_at > timedelta(hours=DEP_TIMEOUT_HOURS):
            run.status = SchedulerRunStatus.DEP_TIMEOUT
            run.message = f"依赖超过 {DEP_TIMEOUT_HOURS}h 未成功"
            stats["dep_timeout"] += 1
            continue
        ok, reason = await _dep_satisfied(db, task, run.ds)
        if not ok:
            run.message = reason
            stats["waiting"] += 1
            continue
        await _execute_run(db, run, task)
        stats["ran" if run.status in (SchedulerRunStatus.SUCCESS, SchedulerRunStatus.FAILED)
              else "blocked"] += 1
    return stats


async def tick(db: AsyncSession, now: datetime | None = None) -> dict:
    """每分钟调度节拍：到期任务生成实例 → 等待中的实例做依赖放行与执行。"""
    now = now or local_now()
    ds = business_day(now)
    stats = {"created": 0, "ran": 0, "blocked": 0, "waiting": 0, "dep_timeout": 0}

    tasks = (await db.execute(select(EtlTask).where(EtlTask.sched_enabled.is_(True)))).scalars().all()
    by_id = {str(t.id): t for t in tasks}

    # 1) cron 到期 → 生成运行实例（同任务同 ds 同触发时刻去重；
    #    一天多次的 cron 每个时点各跑一次，失败不影响后续时点补跑）
    for t in tasks:
        slot = cron_fire_slot(t.cron, now)
        if slot is None:
            continue
        dup = (await db.execute(select(SchedulerRun).where(
            SchedulerRun.task_id == t.id, SchedulerRun.ds == ds,
            SchedulerRun.slot == slot))).scalars().first()
        if dup is not None:
            continue
        db.add(SchedulerRun(task_id=t.id, ds=ds, slot=slot,
                            status=SchedulerRunStatus.WAITING,
                            trigger_type="scheduled"))
        stats["created"] += 1
    await db.flush()

    # 2) 等待中的实例：依赖检查 → 放行执行 / 超时标记
    stats.update(await _process_waiting(db, by_id))
    return stats


def missed_fire_times(expr: str, start: datetime, end: datetime,
                      limit: int = CATCHUP_MAX_PER_TASK) -> list[datetime]:
    """枚举 cron 在 (start, end] 内错过的触发时刻（naive 本地时间，升序），limit 截断。"""
    trig = _cron_trigger(expr)
    if trig is None:
        return []
    out: list[datetime] = []
    t = trig.get_next_fire_time(None, start)
    while t is not None and len(out) < limit:
        t = t.replace(tzinfo=None, second=0, microsecond=0)
        if t > end:
            break
        if not out or t > out[-1]:
            out.append(t)
        # cron 最小粒度 1 分钟：+61s 必越过当前触发点
        t = trig.get_next_fire_time(None, t + timedelta(seconds=61))
    return out


async def catchup(db: AsyncSession, now: datetime | None = None) -> dict:
    """停机补偿：启动/恢复时回补窗口内错过的 cron 时点（按序生成实例）。

    - 窗口为 settings.scheduler_catchup_hours（默认 24h，硬上限 30 天），
      防止长停机（沉睡一年）后回补量爆炸；
    - 幂等：与 tick 共用 (task_id, ds, slot) 去重，重复回补/与正常节拍重叠不产生重复实例；
    - 回补实例与存量等待实例一起走依赖闸门与双重闸门（阻塞即阻塞，不跳过）。
    """
    now = now or local_now()
    hours = int(getattr(settings, "scheduler_catchup_hours", 24) or 24)
    hours = max(0, min(hours, 24 * 30))
    stats = {"created": 0, "skipped_dup": 0, "ran": 0, "blocked": 0,
             "waiting": 0, "dep_timeout": 0}
    if hours <= 0:
        return stats
    start = now - timedelta(hours=hours)

    tasks = (await db.execute(select(EtlTask).where(EtlTask.sched_enabled.is_(True)))).scalars().all()
    by_id = {str(t.id): t for t in tasks}
    for t in tasks:
        for fire in missed_fire_times(t.cron, start, now):
            ds_day = business_day(fire)
            slot = fire.strftime("%H:%M")
            dup = (await db.execute(select(SchedulerRun.id).where(
                SchedulerRun.task_id == t.id, SchedulerRun.ds == ds_day,
                SchedulerRun.slot == slot))).first()
            if dup is not None:
                stats["skipped_dup"] += 1
                continue
            db.add(SchedulerRun(task_id=t.id, ds=ds_day, slot=slot,
                                status=SchedulerRunStatus.WAITING,
                                trigger_type="scheduled"))
            stats["created"] += 1
    await db.flush()
    stats.update({k: v for k, v in (await _process_waiting(db, by_id)).items()})
    return stats


def _collect_downstream(tasks: list[EtlTask], root_id) -> list[EtlTask]:
    """按 depends_on DAG 收集 root 的全部下游并拓扑排序（BFS 收集 + visited 环防护）。

    返回顺序保证：任一任务在其下游集合内部依赖的任务之后出现（Kahn 拓扑序），
    这样级联触发时上游实例先落库，下游依赖检查能看到最新状态。
    """
    children: dict[str, list[EtlTask]] = {}
    for t in tasks:
        for dep in (t.depends_on or []):
            if dep:
                children.setdefault(str(dep), []).append(t)
    # BFS 收集下游集合（visited 去重即环防护，成环也不会死循环）
    seen: set[str] = set()
    queue = [str(root_id)]
    while queue:
        cur = queue.pop(0)
        for child in children.get(cur, []):
            cid = str(child.id)
            if cid in seen:
                continue
            seen.add(cid)
            queue.append(cid)
    seen.discard(str(root_id))
    subset = {str(t.id): t for t in tasks if str(t.id) in seen}
    # Kahn 拓扑排序（仅统计集合内部边）
    indeg = {tid: 0 for tid in subset}
    for tid, t in subset.items():
        for dep in (t.depends_on or []):
            if str(dep) in subset:
                indeg[tid] += 1
    order: list[EtlTask] = []
    ready = [tid for tid, d in indeg.items() if d == 0]
    while ready:
        tid = ready.pop(0)
        order.append(subset[tid])
        for child in children.get(tid, []):
            cid = str(child.id)
            if cid in indeg:
                indeg[cid] -= 1
                if indeg[cid] == 0:
                    ready.append(cid)
    # 兜底：环内未拓扑出的节点按序附上（各自仍走依赖闸门，会被闸门拦住）
    placed = {str(t.id) for t in order}
    order.extend(t for tid, t in subset.items() if tid not in placed)
    return order


async def trigger(db: AsyncSession, task: EtlTask, user, ds: str = "",
                  cascade: bool = False) -> SchedulerRun | list[dict]:
    """手动触发（立即运行）：同样走依赖检查与双重闸门。

    cascade=False（默认，向后兼容）：仅触发本任务，返回运行实例 SchedulerRun。
    cascade=True：按 depends_on DAG 拓扑序连带触发该任务的全部下游
      （BFS 收集 + visited 环防护，环依赖不会死循环）；每个任务仍各自走
      依赖检查与双重闸门（阻塞即阻塞，不跳过）；返回触发结果清单
      [{task_id, task_name, instance_id, status, message}]（首元素为本任务）。
    """
    # 与 tick 口径一致用 local_now()：服务器本地时区非配置时区时，
    # datetime.now() 算出的 ds 会与定时节拍差一天
    ds = ds or business_day(local_now())

    async def _fire(t: EtlTask) -> SchedulerRun:
        run = SchedulerRun(task_id=t.id, ds=ds, status=SchedulerRunStatus.WAITING,
                           trigger_type="manual")
        db.add(run)
        await db.flush()
        ok, reason = await _dep_satisfied(db, t, ds)
        if not ok:
            run.message = reason
        else:
            await _execute_run(db, run, t)
        return run

    run = await _fire(task)
    db.add(AuditLog(user_id=user.id, username=user.username, action="scheduler.trigger",
                    resource_type="etl_task", resource_id=str(task.id),
                    detail={"ds": ds, "status": run.status.value, "message": run.message[:200]}))
    if not cascade:
        return run

    results = [{"task_id": str(task.id), "task_name": task.name or task.target_table,
                "instance_id": str(run.id), "status": run.status.value,
                "message": run.message[:200]}]
    all_tasks = (await db.execute(select(EtlTask))).scalars().all()
    for down in _collect_downstream(list(all_tasks), task.id):
        r = await _fire(down)
        db.add(AuditLog(user_id=user.id, username=user.username, action="scheduler.trigger",
                        resource_type="etl_task", resource_id=str(down.id),
                        detail={"ds": ds, "status": r.status.value, "cascade": True,
                                "message": r.message[:200]}))
        results.append({"task_id": str(down.id), "task_name": down.name or down.target_table,
                        "instance_id": str(r.id), "status": r.status.value,
                        "message": r.message[:200]})
    return results


async def retry(db: AsyncSession, run: SchedulerRun, user) -> SchedulerRun:
    """失败/阻塞实例重跑（新实例，trigger=retry）。"""
    task = await db.get(EtlTask, run.task_id)
    if task is None:
        run.status = SchedulerRunStatus.FAILED
        run.message = "任务已删除"
        return run
    new = SchedulerRun(task_id=task.id, ds=run.ds, status=SchedulerRunStatus.WAITING,
                       trigger_type="retry")
    db.add(new)
    await db.flush()
    ok, reason = await _dep_satisfied(db, task, run.ds)
    if not ok:
        new.message = reason
    else:
        await _execute_run(db, new, task)
    db.add(AuditLog(user_id=user.id, username=user.username, action="scheduler.retry",
                    resource_type="etl_task", resource_id=str(task.id),
                    detail={"ds": run.ds, "status": new.status.value}))
    return new
