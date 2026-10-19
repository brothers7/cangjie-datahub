"""PRD 4.4 安全加固：登录限流 / LIKE 转义 / 只读会话开关接线 / 引擎缓存清理 / TLS 校验开关。"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.security import hash_password
from app.core.utils import like_contains, like_escape
from app.db.models import (AirflowConfig, AzkabanConfig, DataSource, DbType, MetaTable, Role,
                           TableLayer, User)


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


class TestLoginRateLimit:
    async def test_lockout_after_five_failures(self, app_client, committed):
        user = await _mk_user(committed, f"lock-{uuid.uuid4().hex[:8]}")

        for i in range(4):   # 前 4 次：普通错误提示，不锁定
            r = await app_client.post("/api/v1/auth/login",
                                      json={"username": user.username, "password": "wrong"})
            assert r.status_code in (400, 401)
            assert "锁定" not in r.json()["message"]
        # 第 5 次触发锁定
        r = await app_client.post("/api/v1/auth/login",
                                  json={"username": user.username, "password": "wrong"})
        assert r.status_code in (400, 401)
        # 锁定中即使密码正确也拒绝
        r = await app_client.post("/api/v1/auth/login",
                                  json={"username": user.username, "password": "pw123456"})
        assert r.status_code in (400, 401)
        assert "锁定" in r.json()["message"]

        # 锁定状态入库
        await committed.refresh(user)
        assert user.locked_until is not None
        # 清理锁定，避免影响同库其他用例
        user.locked_until = None
        user.failed_login_count = 0
        await committed.commit()

    async def test_failure_count_resets_on_success(self, app_client, committed):
        user = await _mk_user(committed, f"cnt-{uuid.uuid4().hex[:8]}")
        for _ in range(2):
            await app_client.post("/api/v1/auth/login",
                                  json={"username": user.username, "password": "wrong"})
        await committed.refresh(user)
        assert user.failed_login_count == 2
        r = await app_client.post("/api/v1/auth/login",
                                  json={"username": user.username, "password": "pw123456"})
        assert r.status_code == 200
        await committed.refresh(user)
        assert user.failed_login_count == 0 and user.locked_until is None

    async def test_unknown_user_not_counted(self, app_client):
        # 不存在的账号同样返回模糊错误（不产生计数行/不探测用户名）
        r = await app_client.post("/api/v1/auth/login",
                                  json={"username": f"nope-{uuid.uuid4().hex[:8]}",
                                        "password": "wrong"})
        assert r.status_code in (400, 401)
        assert r.json()["message"] == "用户名或密码错误"


class TestLikeEscape:
    def test_escape(self):
        assert like_escape("a%b_c\\d") == "a\\%b\\_c\\\\d"
        assert like_contains("100%") == "%100\\%%"

    async def test_task_metadata_search_is_literal(self, app_client, committed):
        admin = await _mk_user(committed, f"like-{uuid.uuid4().hex[:8]}")
        ds = DataSource(name=f"仓-{uuid.uuid4().hex[:8]}", db_type=DbType.MYSQL,
                        host="127.0.0.1", port=3306, database_name="d", username="r")
        committed.add(ds)
        await committed.flush()
        tag = uuid.uuid4().hex[:6]
        # "a%b" 里的 % 若未转义会同时匹配 "axb"；转义后只命中字面表
        for name in (f"t_{tag}_a%b", f"t_{tag}_axb"):
            committed.add(MetaTable(datasource_id=ds.id, table_name=name,
                                    layer=TableLayer.ODS))
        await committed.commit()
        ah = await _login(app_client, admin.username)

        r = await app_client.get(f"/api/v1/modeling/task-metadata?q=t_{tag}_a%25b", headers=ah)
        names = [t["name"] for t in r.json()["tables"]]
        assert f"t_{tag}_a%b" in names and f"t_{tag}_axb" not in names
        # 清理数据源，避免影响其他用例的列表计数断言
        await committed.delete(ds)
        await committed.commit()


class TestReadonlyGate:
    def test_mysql_readonly_toggle(self):
        from app.connectors.registry import MySQLConnector
        ds = DataSource(name="t", db_type=DbType.MYSQL, host="h", port=3306,
                        database_name="d", username="u", readonly_enforced=True)
        conn = MySQLConnector(ds)
        stmts = conn._session_statements()
        assert any("READ ONLY" in s for s in stmts)
        assert any("max_execution_time" in s for s in stmts)

        ds.readonly_enforced = False
        stmts = conn._session_statements()
        assert not any("READ ONLY" in s for s in stmts)
        assert any("max_execution_time" in s for s in stmts)   # 超时始终保留

    def test_postgres_readonly_toggle(self):
        from app.connectors.registry import PostgresConnector
        ds = DataSource(name="t", db_type=DbType.POSTGRESQL, host="h", port=5432,
                        database_name="d", username="u", readonly_enforced=False)
        stmts = PostgresConnector(ds)._session_statements()
        assert not any("READ ONLY" in s for s in stmts)
        assert any("statement_timeout" in s for s in stmts)


class TestEngineCache:
    def test_drop_cached_engine(self):
        from app.connectors.base import Connector

        class _Fake:
            disposed = False
            def dispose(self):
                self.disposed = True

        ds_id = uuid.uuid4().hex
        fake = _Fake()
        cache = getattr(Connector, "_engine_cache", {}) or {}
        cache[(ds_id, 1.0)] = fake
        Connector._engine_cache = cache
        try:
            Connector.drop_cached_engine(ds_id)
            assert (ds_id, 1.0) not in cache
            assert fake.disposed
        finally:
            cache.clear()

    async def test_delete_datasource_clears_cache(self, app_client, committed):
        admin = await _mk_user(committed, f"del-{uuid.uuid4().hex[:8]}")
        ds = DataSource(name=f"删-{uuid.uuid4().hex[:8]}", db_type=DbType.SQLITE,
                        database_name=":memory:", username="")
        committed.add(ds)
        await committed.commit()
        committed._test_created.append(ds)

        from app.connectors.base import Connector

        class _Fake:
            def dispose(self):
                pass

        cache = getattr(Connector, "_engine_cache", {}) or {}
        cache[(str(ds.id), 1.0)] = _Fake()
        Connector._engine_cache = cache

        ah = await _login(app_client, admin.username)
        r = await app_client.delete(f"/api/v1/datasources/{ds.id}", headers=ah)
        assert r.status_code == 200
        assert not any(k[0] == str(ds.id) for k in cache)


class TestVerifyTls:
    async def test_airflow_config_roundtrip(self, app_client, committed):
        admin = await _mk_user(committed, f"tls-a-{uuid.uuid4().hex[:8]}")
        ah = await _login(app_client, admin.username)

        r = await app_client.put("/api/v1/airflow/config", headers=ah,
                                 json={"url": "https://af.example.com", "username": "u",
                                       "password": "p", "verify_tls": True})
        assert r.status_code == 200
        assert r.json()["verify_tls"] is True
        r = await app_client.get("/api/v1/airflow/config", headers=ah)
        assert r.json()["verify_tls"] is True

        # 客户端构造带上开关
        cfg = (await committed.execute(select(AirflowConfig))).scalars().first()
        from app.services import airflow as af
        assert af.client_from_config(cfg).verify_tls is True

        # 关掉后回读为 False
        r = await app_client.put("/api/v1/airflow/config", headers=ah,
                                 json={"url": "https://af.example.com", "username": "u",
                                       "verify_tls": False})
        assert r.json()["verify_tls"] is False
        # 清理（单行语义配置，避免影响其他用例）
        await committed.delete(cfg)
        await committed.commit()

    async def test_azkaban_config_roundtrip(self, app_client, committed):
        admin = await _mk_user(committed, f"tls-z-{uuid.uuid4().hex[:8]}")
        ah = await _login(app_client, admin.username)

        r = await app_client.put("/api/v1/azkaban/config", headers=ah,
                                 json={"url": "https://azk.example.com", "username": "u",
                                       "password": "p", "default_project": "p1",
                                       "verify_tls": True})
        assert r.status_code == 200
        assert r.json()["verify_tls"] is True

        cfg = (await committed.execute(select(AzkabanConfig))).scalars().first()
        from app.services import azkaban as azk
        assert azk.client_from_config(cfg).verify_tls is True
        await committed.delete(cfg)
        await committed.commit()
