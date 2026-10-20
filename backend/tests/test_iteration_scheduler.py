"""本轮迭代测试：时区配置化 / 手动触发级联下游 / 停机补偿（catch-up）/ 扫描补采建表时间与存储格式。"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.connectors import registry as conn_registry
from app.connectors.base import ColumnMeta, TableMeta
from app.core.config import settings
from app.core.security import hash_password
from app.db.models import (DataSource, DbType, EtlTask, MetaTable, Role, ScanMode,
                           SchedulerRun, SchedulerRunStatus, User)
from app.metadata import scanner as scanner_mod
from app.metadata.scanner import scan_datasource
from app.services import scheduler_engine as eng


async def _mk_user(session) -> User:
    u = User(username=f"it-{uuid.uuid4().hex[:8]}", password_hash=hash_password("pw123456"),
             display_name="it", role=Role.ADMIN)
    session.add(u)
    await session.flush()
    return u


def _sqlite_ds(name: str, path: str, write: bool = True) -> DataSource:
    return DataSource(name=name, db_type=DbType.SQLITE, database_name=path,
                      extra_params={"path": path}, sched_write_enabled=write)


async def _mk_task(session, owner, **kw) -> EtlTask:
    t = EtlTask(user_id=owner.id, name=kw.pop("name", "任务"),
                target_table=kw.pop("target_table", f"t_{uuid.uuid4().hex[:6]}"),
                cron=kw.pop("cron", ""), **kw)
    session.add(t)
    await session.flush()
    return t


def _dw_file(tmp_path, tables: list[str]) -> str:
    dbfile = str(tmp_path / "dw.db")
    conn = sqlite3.connect(dbfile)
    for t in tables:
        conn.execute(f"CREATE TABLE {t} (v INTEGER)")
    conn.commit()
    conn.close()
    return dbfile


def _count(dbfile: str, table: str) -> int:
    conn = sqlite3.connect(dbfile)
    n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.close()
    return n


class TestTimezone:
    def test_local_now_respects_app_tz(self, monkeypatch):
        """app_tz=UTC 时 local_now 与 UTC 墙钟一致；配置缺失时回退 Asia/Shanghai。"""
        monkeypatch.setattr(settings, "app_tz", "UTC")
        got = eng.local_now()
        expect = datetime.now(timezone.utc).replace(tzinfo=None)
        assert abs((got - expect).total_seconds()) < 5

        monkeypatch.setattr(settings, "app_tz", "Asia/Shanghai")
        got = eng.local_now()
        sh = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
        assert abs((got - sh).total_seconds()) < 5


class TestCascadeTrigger:
    async def test_chain_cascade_success(self, session, tmp_path):
        """A→B→C 链：cascade 触发 A 后按拓扑序生成 B、C 实例并执行成功。"""
        owner = await _mk_user(session)
        dbfile = _dw_file(tmp_path, ["ta", "tb", "tc"])
        ds = _sqlite_ds(f"dw-{uuid.uuid4().hex[:8]}", dbfile)
        session.add(ds)
        await session.flush()
        a = await _mk_task(session, owner, name="A", datasource_id=ds.id,
                           etl_sql="INSERT INTO ta SELECT 1", approved_by="admin")
        b = await _mk_task(session, owner, name="B", datasource_id=ds.id,
                           etl_sql="INSERT INTO tb SELECT 1",
                           depends_on=[str(a.id)], approved_by="admin")
        await _mk_task(session, owner, name="C", datasource_id=ds.id,
                       etl_sql="INSERT INTO tc SELECT 1",
                       depends_on=[str(b.id)], approved_by="admin")

        results = await eng.trigger(session, a, owner, ds="2026-10-19", cascade=True)
        # 拓扑序：A → B → C，每个任务一个实例且全部成功
        assert [r["task_name"] for r in results] == ["A", "B", "C"]
        assert all(r["status"] == "success" for r in results), results
        assert len({r["instance_id"] for r in results}) == 3
        # 只统计本次触发的实例（共享库中可能有其他用例残留的 SchedulerRun）
        instance_ids = {r["instance_id"] for r in results}
        runs = (await session.execute(
            select(SchedulerRun).where(SchedulerRun.id.in_(instance_ids))
        )).scalars().all()
        assert len(runs) == 3
        for t in ("ta", "tb", "tc"):
            assert _count(dbfile, t) == 1

    async def test_cycle_no_hang(self, session):
        """环依赖：cascade 不死循环，环上任务各生成一个实例。"""
        owner = await _mk_user(session)
        a = await _mk_task(session, owner, name="A")
        b = await _mk_task(session, owner, name="B", depends_on=[str(a.id)])
        a.depends_on = [str(b.id)]  # 直接 ORM 构造环（API 层会拦截，引擎需容错）
        await session.flush()

        results = await eng.trigger(session, a, owner, ds="2026-10-19", cascade=True)
        assert len(results) == 2
        assert len({r["instance_id"] for r in results}) == 2

    async def test_blocked_unapproved_and_backward_compat(self, session):
        """未审核任务阻塞（不跳过）；cascade 默认 False 时签名向后兼容返回单实例。"""
        owner = await _mk_user(session)
        a = await _mk_task(session, owner, name="A")
        await _mk_task(session, owner, name="B", depends_on=[str(a.id)])

        # 默认 cascade=False：返回 SchedulerRun 单实例（API 层旧调用不变）
        run = await eng.trigger(session, a, owner, ds="2026-10-19")
        assert isinstance(run, SchedulerRun)
        assert run.status == SchedulerRunStatus.BLOCKED and "未审核" in run.message

        # cascade=True：A 阻塞即阻塞；B 因 A 未成功而等待依赖
        results = await eng.trigger(session, a, owner, ds="2026-10-19", cascade=True)
        assert results[0]["status"] == "blocked" and "未审核" in results[0]["message"]
        assert results[1]["status"] == "waiting" and "等待依赖" in results[1]["message"]


class TestCatchup:
    async def test_backfill_missed_slot(self, session, tmp_path):
        """2 小时前错过的 cron 时点：启动回补生成实例并走双重闸门执行；重复回补幂等。"""
        owner = await _mk_user(session)
        dbfile = _dw_file(tmp_path, ["ads_c"])
        ds = _sqlite_ds(f"dw-{uuid.uuid4().hex[:8]}", dbfile)
        session.add(ds)
        await session.flush()
        task = await _mk_task(session, owner, name="回补", cron="30 8 * * *",
                              datasource_id=ds.id, etl_sql="INSERT INTO ads_c SELECT 1",
                              approved_by="admin", sched_enabled=True)
        now = datetime(2026, 10, 20, 10, 30)  # 当天 08:30 时点为 2 小时前（窗口 24h 内）

        stats = await eng.catchup(session, now)
        assert stats["created"] == 1
        run = (await session.execute(
            select(SchedulerRun).where(SchedulerRun.task_id == task.id))).scalars().one()
        assert run.ds == "2026-10-19" and run.slot == "08:30"
        assert run.trigger_type == "scheduled"
        assert run.status == SchedulerRunStatus.SUCCESS, run.message
        assert _count(dbfile, "ads_c") == 1

        # 幂等：重复回补（或与 tick 重叠）不产生重复实例
        stats2 = await eng.catchup(session, now)
        assert stats2["created"] == 0 and stats2["skipped_dup"] == 1
        runs = (await session.execute(
            select(SchedulerRun).where(SchedulerRun.task_id == task.id))).scalars().all()
        assert len(runs) == 1

    async def test_out_of_window_not_backfilled(self, session, monkeypatch):
        """超出补偿窗口的时点不补。"""
        owner = await _mk_user(session)
        task = await _mk_task(session, owner, name="超窗", cron="30 8 * * *",
                              etl_sql="INSERT INTO x SELECT 1", sched_enabled=True)
        monkeypatch.setattr(settings, "scheduler_catchup_hours", 1)
        now = datetime(2026, 10, 20, 10, 30)  # 08:30 在 2h 前，超出 1h 窗口

        stats = await eng.catchup(session, now)
        assert stats["created"] == 0
        runs = (await session.execute(
            select(SchedulerRun).where(SchedulerRun.task_id == task.id))).scalars().all()
        assert runs == []

    def test_missed_fire_times_bounded_and_ordered(self):
        """枚举升序且受 limit 截断（防沉睡一年补一万条）。"""
        start = datetime(2026, 10, 19, 10, 30)
        end = datetime(2026, 10, 20, 10, 30)
        fires = eng.missed_fire_times("30 8 * * *", start, end)
        assert fires == [datetime(2026, 10, 20, 8, 30)]
        hourly = eng.missed_fire_times("0 * * * *", start, end, limit=3)
        assert len(hourly) == 3 and hourly == sorted(hourly)
        assert eng.missed_fire_times("bad cron", start, end) == []


class _FakeConnector:
    """mock 连接器：返回固定元数据，不连真实库。"""

    def __init__(self, metas):
        self._metas = metas

    async def introspect(self):
        return self._metas

    async def sample_column_stats(self, table, columns):
        return {}


class TestScanStorageInfo:
    async def _scan_one(self, session, monkeypatch, tm: TableMeta, db_type) -> MetaTable:
        monkeypatch.setattr(scanner_mod, "get_connector", lambda ds: _FakeConnector([tm]))
        ds = DataSource(name=f"mock-{uuid.uuid4().hex[:8]}", db_type=db_type,
                        database_name="default", password_cipher="", username="")
        session.add(ds)
        await session.flush()
        report = await scan_datasource(session, ds, ScanMode.FULL)
        assert report["errors"] == []
        return (await session.execute(
            select(MetaTable).where(MetaTable.datasource_id == ds.id))).scalars().one()

    async def test_create_time_and_storage_format_persisted(self, session, monkeypatch):
        """mock 连接器返回建表时间/存储格式 → MetaTable 正确落库。"""
        ct = datetime(2024, 1, 2, 3, 4, 5)
        tm = TableMeta(schema_name="", table_name="ads_x", comment="x",
                       columns=[ColumnMeta(column_name="id", data_type="INT")],
                       create_time=ct, storage_format="Parquet")
        row = await self._scan_one(session, monkeypatch, tm, DbType.HIVE)
        assert row.create_time.replace(tzinfo=None) == ct
        assert row.storage_format == "Parquet"

    async def test_unavailable_fields_stay_null(self, session, monkeypatch):
        """连接器取不到（SQLite 等方言留空）→ 两列为 NULL，不影响扫描。"""
        tm = TableMeta(schema_name="", table_name="t1",
                       columns=[ColumnMeta(column_name="id", data_type="INT")])
        row = await self._scan_one(session, monkeypatch, tm, DbType.SQLITE)
        assert row.create_time is None
        assert row.storage_format is None

    def test_parse_hive_storage_format(self):
        """DESCRIBE FORMATTED 结果解析：常见 InputFormat 映射、未知取原名、缺失留空。"""
        rows = [("# Storage Information", None, None),
                ("SerDe Library:", "org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe", None),
                ("InputFormat:", "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat", None)]
        assert conn_registry._parse_hive_storage_format(rows) == "Parquet"
        orc = [("InputFormat", "org.apache.hadoop.hive.ql.io.orc.OrcInputFormat", "")]
        assert conn_registry._parse_hive_storage_format(orc) == "ORC"
        unknown = [("InputFormat", "com.custom.FancyInputFormat", "")]
        assert conn_registry._parse_hive_storage_format(unknown) == "com.custom.FancyInputFormat"
        assert conn_registry._parse_hive_storage_format([("a", "b")]) is None

    def test_create_time_failure_returns_none(self):
        """建表时间补采失败（无驱动/无权限）容错留空，不炸扫描。"""
        class _BadConn:
            def engine(self):
                raise RuntimeError("no driver")

        assert conn_registry._information_schema_create_time(_BadConn(), "t") is None
