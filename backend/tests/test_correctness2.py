"""第二批后端正确性加固：

1. 查询缓存落库存脱敏后的行数据（缓存键含脱敏规则签名，规则变更不错误命中）
2. QueryRecord.sql_hash 接线（夜间反思可按同类查询聚合）
3. 方言感知标识符引用（MySQL 系反引号，其余双引号）
4. 手动触发调度与 tick 时区口径一致（local_now）
5. LLM 配置 create/update 后失效网关缓存
6. /llm/usage 改 SQL 聚合且返回结构不变
7. 扫描任务进程内互斥（409）+ 悬挂任务启动回收
8. bootstrap/ lifespan 真正 await 引擎 dispose，不残留双引擎
"""
from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.security import hash_password
from app.db.models import (DataSource, DbType, EtlTask, Role, ScanJob, ScanJobStatus,
                           ScanMode, User)
from app.db.models.base import utcnow


@pytest.fixture
async def app_client(engine):
    from app.main import create_app
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _mk_user(session, username, role=Role.ADMIN):
    u = User(username=username, password_hash=hash_password("pw123456"),
             display_name=username, role=role)
    session.add(u)
    await session.commit()
    session._test_created.append(u)
    return u


async def _login(client, username):
    r = await client.post("/api/v1/auth/login", json={"username": username, "password": "pw123456"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def _mk_ds(committed, **kw) -> DataSource:
    ds = DataSource(name=f"ds-{uuid.uuid4().hex[:8]}", db_type=DbType.SQLITE,
                    database_name=kw.pop("database_name", ":memory:"), username="", **kw)
    committed.add(ds)
    await committed.commit()
    committed._test_created.append(ds)
    return ds


# ---------- 1+2. 缓存脱敏落库 + sql_hash ----------

class TestCacheMaskingAndSqlHash:
    async def test_finalize_caches_masked_rows_and_fills_sql_hash(self, committed):
        from app.agent.pipeline.context import PipelineContext
        from app.agent.pipeline.orchestrator import _cache_key, _finalize
        from app.connectors.base import QueryResult
        from app.db.models import Conversation, Message, QueryCache, QueryRecord
        from app.kg.service import LinkedEntities
        from app.services import cache_service

        user = await _mk_user(committed, f"cm-{uuid.uuid4().hex[:6]}")
        ds = await _mk_ds(committed)
        conv = Conversation(user_id=user.id, title="t", datasource_id=ds.id)
        committed.add(conv)
        await committed.commit()
        committed._test_created.append(conv)

        ctx = PipelineContext(user_id=str(user.id), username=user.username,
                              conversation_id=str(conv.id), question="各手机号GMV")
        ctx.datasource = ds
        ctx.intent = "metric_query"
        ctx.rewritten = ctx.question
        ctx.linked = LinkedEntities()   # 空链接：只走记录/缓存分支，跳过记忆回写
        ctx.guarded_sql = "SELECT buyer_phone, gmv FROM dwd_orders"
        ctx.query = QueryResult(columns=["buyer_phone", "gmv"],
                                rows=[{"buyer_phone": "13800001111", "gmv": 100.5}],
                                row_count=1, duration_ms=5)
        # 脱敏只作用于输出链路；finalize 缓存的必须是这份脱敏后的行
        ctx.cleaned_rows = [{"buyer_phone": "138***11", "gmv": 100.5}]
        ctx._mask_rules = [("buyer_phone", "mask_partial")]
        ctx.answer_text = "解读"

        await _finalize(ctx, success=True)

        key = _cache_key(ctx)
        try:
            cache_row = (await committed.execute(
                select(QueryCache).where(QueryCache.cache_key == key))).scalar_one()
            assert cache_row.result["rows"][0]["buyer_phone"] == "138***11"
            assert "13800001111" not in str(cache_row.result)
            assert cache_row.result["columns"] == ["buyer_phone", "gmv"]

            qr = (await committed.execute(
                select(QueryRecord).where(QueryRecord.conversation_id == conv.id))).scalar_one()
            assert qr.sql_hash  # 非空：sediment_report_templates 不再恒返回 0
            # sql_hash 不含脱敏签名：同 SQL 同类查询聚合口径
            assert qr.sql_hash == cache_service.cache_key(str(ds.id), ctx.guarded_sql)
        finally:
            # _finalize 走独立会话真实提交，需手工清理，避免影响其他用例
            await committed.execute(delete(QueryCache).where(QueryCache.cache_key == key))
            await committed.execute(
                delete(QueryRecord).where(QueryRecord.conversation_id == conv.id))
            await committed.execute(delete(Message).where(Message.conversation_id == conv.id))
            await committed.commit()

    def test_cache_key_carries_mask_rule_signature(self):
        from app.agent.pipeline.context import PipelineContext
        from app.agent.pipeline.orchestrator import _cache_key
        from app.services import cache_service

        ctx = PipelineContext()
        ctx.datasource = SimpleNamespace(id=uuid.uuid4())
        ctx.guarded_sql = "SELECT 1"
        k_plain = _cache_key(ctx)
        ctx._mask_rules = [("phone", "mask_hash")]
        k_masked = _cache_key(ctx)
        # 规则变更 → 缓存键变化：旧缓存（按旧口径脱敏）不再命中
        assert k_plain != k_masked
        # 旧格式缓存键（不含规则签名）与新键不冲突：历史未脱敏缓存不会错误命中
        legacy = cache_service.cache_key(str(ctx.datasource.id), ctx.guarded_sql)
        assert legacy not in (k_plain, k_masked)
        # 规则集合相同（顺序不同）→ 键稳定
        ctx._mask_rules = [("b", "mask_partial"), ("a", "mask_hash")]
        k1 = _cache_key(ctx)
        ctx._mask_rules = [("a", "mask_hash"), ("b", "mask_partial")]
        assert _cache_key(ctx) == k1


# ---------- 3. 方言感知标识符引用 ----------

class TestQuoteIdent:
    def _ds(self, db_type):
        return DataSource(name="t", db_type=db_type, host="h", port=3306,
                          database_name="d", username="u")

    def test_mysql_family_uses_backtick(self):
        from app.connectors.registry import (DorisConnector, MySQLConnector,
                                             StarRocksConnector)
        for cls, t in ((MySQLConnector, DbType.MYSQL), (DorisConnector, DbType.DORIS),
                       (StarRocksConnector, DbType.STARROCKS)):
            assert cls(self._ds(t))._quote_ident("col") == "`col`"

    def test_default_uses_double_quote(self):
        from app.connectors.registry import PostgresConnector, SqliteConnector
        assert SqliteConnector(self._ds(DbType.SQLITE))._quote_ident("col") == '"col"'
        assert PostgresConnector(self._ds(DbType.POSTGRESQL))._quote_ident("col") == '"col"'

    async def test_sample_stats_sql_quoting(self):
        from app.connectors.registry import MySQLConnector, SqliteConnector

        class _FakeResult:
            def mappings(self): return self
            def first(self): return {"d_0": 2, "n_0": 3}
            def scalars(self): return self
            def all(self): return []

        class _FakeConn:
            def __init__(self, rec): self.rec = rec
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, clause, params=None):
                self.rec.append(str(clause))
                return _FakeResult()

        class _FakeEngine:
            def __init__(self, rec): self.rec = rec
            def connect(self): return _FakeConn(self.rec)

        rec: list[str] = []
        conn = MySQLConnector(self._ds(DbType.MYSQL))
        conn.engine = lambda: _FakeEngine(rec)
        stats = await conn.sample_column_stats("orders", ["buyer_phone"])
        assert stats["buyer_phone"]["distinct"] == 2
        assert any("`buyer_phone`" in s for s in rec)
        assert any("`orders`" in s for s in rec)
        assert not any('"buyer_phone"' in s for s in rec)  # 双引号在 MySQL 下是字符串常量

        rec2: list[str] = []
        conn2 = SqliteConnector(self._ds(DbType.SQLITE))
        conn2.engine = lambda: _FakeEngine(rec2)
        await conn2.sample_column_stats("orders", ["buyer_phone"])
        assert any('"buyer_phone"' in s for s in rec2)
        assert not any("`" in s for s in rec2)


# ---------- 4. 手动触发调度时区统一 ----------

class TestTriggerTimezone:
    async def test_trigger_uses_local_now(self, committed, monkeypatch):
        from app.services import scheduler_engine as se

        owner = await _mk_user(committed, f"tz-{uuid.uuid4().hex[:6]}")
        task = EtlTask(user_id=owner.id, name="tz任务",
                       target_table=f"t_{uuid.uuid4().hex[:6]}", cron="* * * * *")
        committed.add(task)
        await committed.commit()

        # 伪造调度本地时间：与真实系统时间的「昨日」必然不同
        fake_now = datetime(2026, 1, 15, 12, 0, 0)
        monkeypatch.setattr(se, "local_now", lambda: fake_now)
        run = await se.trigger(committed, task, owner)
        # 手动触发与 tick 一样按 local_now() 计算 ds（T+1：前一日）
        assert run.ds == se.business_day(fake_now) == "2026-01-14"


# ---------- 5. LLM 配置缓存一致 ----------

class TestLlmConfigCacheInvalidation:
    async def test_create_invalidates(self, app_client, committed, monkeypatch):
        calls: list[int] = []
        monkeypatch.setattr("app.api.v1.llm.invalidate_config_cache",
                            lambda: calls.append(1))
        admin = await _mk_user(committed, f"lc-{uuid.uuid4().hex[:6]}")
        ah = await _login(app_client, admin.username)
        r = await app_client.post("/api/v1/llm/configs", headers=ah, json={
            "name": f"cfg-{uuid.uuid4().hex[:8]}", "protocol": "openai", "kind": "chat",
            "endpoint": "http://127.0.0.1:1/v1", "api_key": "sk", "model": "m1"})
        assert r.status_code == 200, r.text
        assert len(calls) == 1

    async def test_update_invalidates(self, app_client, committed, monkeypatch):
        calls: list[int] = []
        monkeypatch.setattr("app.api.v1.llm.invalidate_config_cache",
                            lambda: calls.append(1))
        admin = await _mk_user(committed, f"lu-{uuid.uuid4().hex[:6]}")
        ah = await _login(app_client, admin.username)
        r = await app_client.post("/api/v1/llm/configs", headers=ah, json={
            "name": f"cfg-{uuid.uuid4().hex[:8]}", "protocol": "openai", "kind": "chat",
            "endpoint": "http://127.0.0.1:1/v1", "api_key": "sk", "model": "m1"})
        assert r.status_code == 200, r.text
        assert len(calls) == 1
        cid = r.json()["id"]
        r = await app_client.patch(f"/api/v1/llm/configs/{cid}", headers=ah,
                                   json={"model": "m2"})
        assert r.status_code == 200, r.text
        assert len(calls) == 2  # create + update 各失效一次


# ---------- 6. /llm/usage SQL 聚合 ----------

class TestLlmUsage:
    async def test_usage_group_by_day_scene_model(self, app_client, committed):
        from app.db.models.llm import CallStatus, LlmCallLog

        # 清掉同库其他用例遗留的调用日志，保证聚合断言精确
        await committed.execute(delete(LlmCallLog))
        await committed.commit()

        admin = await _mk_user(committed, f"ug-{uuid.uuid4().hex[:6]}")
        ah = await _login(app_client, admin.username)
        now = utcnow()
        yesterday = now - timedelta(days=1)
        logs = [
            LlmCallLog(scene="sql_generate", protocol="openai", model="m1",
                       input_tokens=10, output_tokens=5, status=CallStatus.OK,
                       created_at=now),
            LlmCallLog(scene="sql_generate", protocol="openai", model="m1",
                       input_tokens=20, output_tokens=7, status=CallStatus.ERROR,
                       created_at=now),
            LlmCallLog(scene="", protocol="openai", model="m2",
                       input_tokens=3, output_tokens=1, status=CallStatus.TIMEOUT,
                       created_at=yesterday),
        ]
        for lg in logs:
            committed.add(lg)
            committed._test_created.append(lg)
        await committed.commit()

        # 按天：结构 {key, input_tokens, output_tokens, calls, errors}
        r = await app_client.get("/api/v1/llm/usage?group_by=day&days=14", headers=ah)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["group_by"] == "day"
        rows = {b["key"]: b for b in body["rows"]}
        today_key = now.strftime("%Y-%m-%d")
        y_key = yesterday.strftime("%Y-%m-%d")
        assert rows[today_key] == {"key": today_key, "input_tokens": 30,
                                   "output_tokens": 12, "calls": 2, "errors": 1}
        assert rows[y_key] == {"key": y_key, "input_tokens": 3,
                               "output_tokens": 1, "calls": 1, "errors": 1}
        assert [b["key"] for b in body["rows"]] == sorted(rows)  # 按 key 升序

        # 按场景：空场景归入 default
        r = await app_client.get("/api/v1/llm/usage?group_by=scene&days=14", headers=ah)
        rows = {b["key"]: b for b in r.json()["rows"]}
        assert rows["sql_generate"]["calls"] == 2 and rows["sql_generate"]["errors"] == 1
        assert rows["default"]["calls"] == 1

        # 按模型
        r = await app_client.get("/api/v1/llm/usage?group_by=model&days=14", headers=ah)
        rows = {b["key"]: b for b in r.json()["rows"]}
        assert rows["m1"]["input_tokens"] == 30 and rows["m1"]["calls"] == 2
        assert rows["m2"]["calls"] == 1


# ---------- 7. 扫描互斥 + 悬挂回收 ----------

class TestScanMutex:
    async def test_conflict_when_running_job(self, app_client, committed, tmp_path):
        admin = await _mk_user(committed, f"sx-{uuid.uuid4().hex[:6]}")
        ah = await _login(app_client, admin.username)
        dbfile = tmp_path / "wh.db"
        sqlite3.connect(dbfile).close()
        ds = await _mk_ds(committed, database_name=str(dbfile),
                          extra_params={"path": str(dbfile)})

        # 进行中的 RUNNING 任务（如定时扫描）→ 手动触发 409
        job = ScanJob(datasource_id=ds.id, mode=ScanMode.FULL, triggered_by="cron:x",
                      status=ScanJobStatus.RUNNING, started_at=utcnow())
        committed.add(job)
        await committed.commit()
        committed._test_created.append(job)
        r = await app_client.post(f"/api/v1/datasources/{ds.id}/scan",
                                  headers=ah, json={"mode": "full"})
        assert r.status_code == 409
        assert r.json()["code"] == "SCAN_IN_PROGRESS"

        # RUNNING 超过悬挂阈值 → 视为崩溃遗留，不阻塞新触发
        job.started_at = utcnow() - timedelta(minutes=31)
        await committed.commit()
        r = await app_client.post(f"/api/v1/datasources/{ds.id}/scan",
                                  headers=ah, json={"mode": "full"})
        assert r.status_code == 200, r.text
        assert r.json()["scan_job_id"]
        # 后台扫描真实执行过：新任务不再 RUNNING（锁也已释放）
        new_job = (await committed.execute(select(ScanJob).where(
            ScanJob.id == uuid.UUID(r.json()["scan_job_id"])))).scalar_one()
        assert new_job.status in (ScanJobStatus.SUCCESS, ScanJobStatus.PARTIAL,
                                  ScanJobStatus.FAILED)
        committed._test_created.append(new_job)

        # 扫描结束后可以再次触发
        r = await app_client.post(f"/api/v1/datasources/{ds.id}/scan",
                                  headers=ah, json={"mode": "incremental"})
        assert r.status_code == 200, r.text
        new_job2 = (await committed.execute(select(ScanJob).where(
            ScanJob.id == uuid.UUID(r.json()["scan_job_id"])))).scalar_one()
        committed._test_created.append(new_job2)

    async def test_conflict_when_lock_held(self, app_client, committed):
        from app.api.v1.datasources import _scan_locks

        admin = await _mk_user(committed, f"sl-{uuid.uuid4().hex[:6]}")
        ah = await _login(app_client, admin.username)
        ds = await _mk_ds(committed)

        lock = _scan_locks.setdefault(str(ds.id), asyncio.Lock())
        await lock.acquire()
        try:
            r = await app_client.post(f"/api/v1/datasources/{ds.id}/scan",
                                      headers=ah, json={"mode": "full"})
            assert r.status_code == 409
        finally:
            lock.release()


class TestReapStaleScanJobs:
    async def test_reap_marks_stale_running_failed(self, committed):
        from app.bootstrap import reap_stale_scan_jobs

        ds = await _mk_ds(committed)
        stale = ScanJob(datasource_id=ds.id, mode=ScanMode.FULL,
                        status=ScanJobStatus.RUNNING,
                        started_at=utcnow() - timedelta(minutes=40))
        fresh = ScanJob(datasource_id=ds.id, mode=ScanMode.FULL,
                        status=ScanJobStatus.RUNNING, started_at=utcnow())
        done = ScanJob(datasource_id=ds.id, mode=ScanMode.FULL,
                       status=ScanJobStatus.SUCCESS,
                       started_at=utcnow() - timedelta(hours=2))
        for j in (stale, fresh, done):
            committed.add(j)
            committed._test_created.append(j)
        await committed.commit()

        n = await reap_stale_scan_jobs(committed)
        assert n == 1
        assert stale.status == ScanJobStatus.FAILED
        assert stale.finished_at is not None
        assert any("回收" in e for e in stale.error_log)
        assert fresh.status == ScanJobStatus.RUNNING   # 进行中的不动
        assert done.status == ScanJobStatus.SUCCESS    # 已结束的不动


# ---------- 8. 引擎真正 dispose ----------

class TestDisposeAppEngine:
    async def test_dispose_really_closes_and_resets(self, engine):
        import app.db.session as db_session
        from app.bootstrap import dispose_app_engine

        old = db_session.get_engine()
        assert db_session._engine is old
        await dispose_app_engine()
        assert db_session._engine is None
        assert db_session._session_factory is None
        # 重建的是全新引擎对象，不残留双引擎
        new = db_session.get_engine()
        assert new is not old
        # 收尾：把本用例重建的引擎也释放，避免泄漏给后续用例
        await dispose_app_engine()
        assert db_session._engine is None
