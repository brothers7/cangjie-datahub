"""PRD 3.3G / 3.3D-1：调度平台配置（Airflow）、建模会话模型选择、内置调度引擎。"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import hash_password
from app.db.models import (DataSource, DbType, EtlTask, LlmConfig, ModelKind, ModelingMessage,
                           ModelingSession, Protocol, Role, SchedulerRun, SchedulerRunStatus, User)
from app.services import scheduler_engine as sched_engine


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


def _sqlite_ds(name: str, path: str, write: bool = False) -> DataSource:
    return DataSource(name=name, db_type=DbType.SQLITE, database_name=path,
                      extra_params={"path": path}, sched_write_enabled=write)


class TestAirflowConfig:
    async def test_config_crud_and_test_fail(self, app_client, committed):
        admin = await _mk_user(committed, f"af-{uuid.uuid4().hex[:6]}")
        analyst = await _mk_user(committed, f"af-a-{uuid.uuid4().hex[:6]}", Role.ANALYST)
        ah = await _login(app_client, admin.username)
        uh = await _login(app_client, analyst.username)

        # 非管理员不可改配置
        r = await app_client.put("/api/v1/airflow/config", headers=uh,
                                 json={"url": "http://x", "username": "u"})
        assert r.status_code == 403

        r = await app_client.put("/api/v1/airflow/config", headers=ah,
                                 json={"url": "http://127.0.0.1:1", "username": "u",
                                       "password": "p"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["configured"] and "password" not in r.text

        # 地址不可达 → 测试失败并落 ERROR 状态
        r = await app_client.post("/api/v1/airflow/test", headers=ah)
        assert r.json()["ok"] is False
        r = await app_client.get("/api/v1/airflow/config", headers=ah)
        assert r.json()["status"] == "error"

        # metadata 容错返回
        r = await app_client.get("/api/v1/airflow/metadata", headers=ah)
        assert r.status_code == 200 and r.json()["configured"] is True


class TestSessionLlm:
    async def test_session_model_select(self, app_client, committed):
        admin = await _mk_user(committed, f"sl-{uuid.uuid4().hex[:6]}")
        cfg = LlmConfig(name=f"m-{uuid.uuid4().hex[:8]}", protocol=Protocol.OPENAI,
                        kind=ModelKind.CHAT, endpoint="http://x", model="m1")
        committed.add(cfg)
        await committed.commit()
        h = await _login(app_client, admin.username)

        # 非法配置 → 422
        r = await app_client.post("/api/v1/modeling/sessions", headers=h,
                                  json={"requirement": "r", "llm_config_id": str(uuid.uuid4())})
        assert r.status_code == 422

        # 合法配置 → 随会话持久化；PATCH 可切换/清除
        r = await app_client.post("/api/v1/modeling/sessions", headers=h,
                                  json={"requirement": "r", "llm_config_id": str(cfg.id)})
        assert r.status_code == 200, r.text
        sid = r.json()["id"]
        assert r.json()["llm_config_id"] == str(cfg.id)
        r = await app_client.patch(f"/api/v1/modeling/sessions/{sid}", headers=h,
                                   json={"llm_config_id": None})
        assert r.status_code == 200 and r.json()["llm_config_id"] is None


class TestAcceptDependency:
    async def test_accept_auto_depends(self, app_client, committed):
        """接受方案落地时按来源表自动推导任务级依赖（含方案内上游表）。"""
        admin = await _mk_user(committed, f"ad-{uuid.uuid4().hex[:6]}")
        s = ModelingSession(user_id=admin.id, title="t", requirement="r")
        committed.add(s)
        await committed.flush()
        suffix = uuid.uuid4().hex[:6]
        proposal = {"tables": [
            {"name": f"dws_a_{suffix}", "layer": "dws", "ddl": "", "etl_sql": "",
             "source_tables": ["dwd_order"]},
            {"name": f"ads_b_{suffix}", "layer": "ads", "ddl": "", "etl_sql": "",
             "source_tables": [f"dws_a_{suffix}"]},
        ], "summary": "x"}
        m = ModelingMessage(session_id=s.id, seq=1, role="assistant", content="x",
                            proposal=proposal)
        committed.add(m)
        await committed.commit()
        h = await _login(app_client, admin.username)

        r = await app_client.post(f"/api/v1/modeling/sessions/{s.id}/accept", headers=h,
                                  json={"message_id": str(m.id)})
        assert r.status_code == 200, r.text
        assert r.json()["created"] == 2

        r = await app_client.get("/api/v1/etl/tasks", headers=h)
        tasks = {t["target_table"]: t for t in r.json()}
        dws, ads = tasks[f"dws_a_{suffix}"], tasks[f"ads_b_{suffix}"]
        assert ads["depends_on"] == [dws["id"]]     # 依赖明确到任务 id
        assert dws["depends_on"] == []               # 来源表无对应任务 → 无依赖


class TestBuiltinScheduler:
    async def _mk_task(self, committed, owner, **kw) -> EtlTask:
        t = EtlTask(user_id=owner.id, name=kw.pop("name", "任务"),
                    target_table=kw.pop("target_table", f"t_{uuid.uuid4().hex[:6]}"),
                    cron=kw.pop("cron", "* * * * *"), **kw)
        committed.add(t)
        await committed.commit()
        return t

    async def test_gates_and_execution(self, committed, tmp_path):
        """双重闸门：未审核/未开放写入 → 阻塞无写动作；双开 → 执行成功。"""
        owner = await _mk_user(committed, f"sc-{uuid.uuid4().hex[:6]}")
        dbfile = str(tmp_path / "dw.db")
        conn = sqlite3.connect(dbfile)
        conn.execute("CREATE TABLE ads_x (v INTEGER)")
        conn.commit()
        conn.close()
        ds = _sqlite_ds(f"dw-{uuid.uuid4().hex[:8]}", dbfile, write=False)
        committed.add(ds)
        await committed.flush()
        task = await self._mk_task(committed, owner, datasource_id=ds.id,
                                   etl_sql="INSERT INTO ads_x SELECT 1",
                                   sched_enabled=True)
        now = datetime(2026, 10, 13, 10, 30)

        # 闸门全关 → 到期生成实例但阻塞（未审核）
        stats = await sched_engine.tick(committed, now)
        assert stats["created"] == 1
        run = (await committed.execute(
            __import__("sqlalchemy").select(SchedulerRun))).scalars().first()
        assert run.status == SchedulerRunStatus.BLOCKED and "未审核" in run.message
        # 无写动作
        conn = sqlite3.connect(dbfile)
        assert conn.execute("SELECT COUNT(*) FROM ads_x").fetchone()[0] == 0
        conn.close()

        # 闸门一（审核）→ 仍阻塞（数据源未开放写入）
        task.approved_by = "admin"
        run2 = SchedulerRun(task_id=task.id, ds=run.ds, trigger_type="manual")
        committed.add(run2)
        await committed.flush()
        await sched_engine._execute_run(committed, run2, task)
        assert run2.status == SchedulerRunStatus.BLOCKED and "未开放" in run2.message

        # 双开 → 执行成功
        ds.sched_write_enabled = True
        run3 = SchedulerRun(task_id=task.id, ds=run.ds, trigger_type="manual")
        committed.add(run3)
        await committed.flush()
        await sched_engine._execute_run(committed, run3, task)
        assert run3.status == SchedulerRunStatus.SUCCESS, run3.message
        conn = sqlite3.connect(dbfile)
        assert conn.execute("SELECT COUNT(*) FROM ads_x").fetchone()[0] == 1
        conn.close()

        # DDL/危险写被写校验拦截（前置闸门已通过）
        bad = await self._mk_task(committed, owner, datasource_id=ds.id,
                                  etl_sql="DROP TABLE ads_x", sched_enabled=True)
        bad.approved_by = "admin"
        run4 = SchedulerRun(task_id=bad.id, ds=run.ds, trigger_type="manual")
        committed.add(run4)
        await committed.flush()
        await sched_engine._execute_run(committed, run4, bad)
        assert run4.status == SchedulerRunStatus.BLOCKED and "INSERT" in run4.message

    async def test_dependency_gating(self, committed):
        """依赖未成功 → 下游等待；上游成功 → 放行（本次因闸门阻塞但已放行依赖）。"""
        owner = await _mk_user(committed, f"sd-{uuid.uuid4().hex[:6]}")
        up = await self._mk_task(committed, owner, name="上游", etl_sql="INSERT INTO x SELECT 1")
        down = await self._mk_task(committed, owner, name="下游", etl_sql="INSERT INTO y SELECT 1",
                                   depends_on=[str(up.id)], sched_enabled=True)
        ds_day = "2026-10-12"
        # 上游无成功实例 → 等待
        ok, reason = await sched_engine._dep_satisfied(committed, down, ds_day)
        assert not ok and "等待依赖" in reason
        # 上游成功 → 放行
        committed.add(SchedulerRun(task_id=up.id, ds=ds_day,
                                   status=SchedulerRunStatus.SUCCESS))
        await committed.flush()
        ok, _ = await sched_engine._dep_satisfied(committed, down, ds_day)
        assert ok

    async def test_cycle_and_approve_and_trigger(self, app_client, committed, tmp_path):
        admin = await _mk_user(committed, f"sa-{uuid.uuid4().hex[:6]}")
        analyst = await _mk_user(committed, f"sa-a-{uuid.uuid4().hex[:6]}", Role.ANALYST)
        a = await self._mk_task(committed, admin, name="A")
        b = await self._mk_task(committed, admin, name="B")
        ah = await _login(app_client, admin.username)
        uh = await _login(app_client, analyst.username)

        # 依赖成环 → 422
        r = await app_client.patch(f"/api/v1/etl/tasks/{a.id}", headers=ah,
                                   json={"depends_on": [str(b.id)]})
        assert r.status_code == 200
        r = await app_client.patch(f"/api/v1/etl/tasks/{b.id}", headers=ah,
                                   json={"depends_on": [str(a.id)]})
        assert r.status_code == 422 and "循环" in r.text

        # 审核仅管理员
        r = await app_client.post(f"/api/v1/scheduler/tasks/{a.id}/approve", headers=uh)
        assert r.status_code == 403
        r = await app_client.post(f"/api/v1/scheduler/tasks/{a.id}/approve", headers=ah)
        assert r.status_code == 200 and r.json()["approved_by"] == admin.username

        # 编辑 SQL → 审核自动失效
        r = await app_client.patch(f"/api/v1/etl/tasks/{a.id}", headers=ah,
                                   json={"etl_sql": "INSERT INTO t SELECT 2"})
        assert r.json()["approved_by"] == ""

        # 手动触发：无依赖任务，未审核 → 阻塞实例 + 可见
        c = await self._mk_task(committed, admin, name="C")
        r = await app_client.post(f"/api/v1/scheduler/tasks/{c.id}/trigger", headers=ah,
                                  json={"ds": "2026-10-12"})
        assert r.status_code == 200, r.text
        run = r.json()["run"]
        assert run["status"] == "blocked" and "未审核" in run["message"]

        # 实例列表 + DAG 总览
        r = await app_client.get(f"/api/v1/scheduler/runs?task_id={c.id}", headers=ah)
        assert len(r.json()["runs"]) >= 1
        await app_client.patch(f"/api/v1/etl/tasks/{a.id}", headers=ah,
                               json={"sched_enabled": True})
        r = await app_client.get("/api/v1/scheduler/dag", headers=ah)
        assert any(n["id"] == str(a.id) for n in r.json()["nodes"])

    async def test_export_airflow(self, app_client, committed):
        admin = await _mk_user(committed, f"se-{uuid.uuid4().hex[:6]}")
        t = await self._mk_task(committed, admin, target_table="ads_exp",
                                etl_sql="INSERT INTO ads_exp SELECT 1", cron="0 2 * * *")
        h = await _login(app_client, admin.username)
        r = await app_client.post(f"/api/v1/etl/tasks/{t.id}/export", headers=h,
                                  json={"scheduler": "airflow"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["filename"] == "ads_exp_dag.py"
        assert "DAG" in body["content"] and "ads_exp" in body["content"]
        # 平台清单包含内置调度与 Airflow
        r = await app_client.get("/api/v1/etl/tasks/schedulers", headers=h)
        keys = [p["key"] for p in r.json()["platforms"]]
        assert "local" in keys and "airflow" in keys
