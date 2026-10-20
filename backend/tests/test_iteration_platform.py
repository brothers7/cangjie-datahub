"""生产就绪迭代·平台层：alembic 基线 / 配置外提 / 生产配置强制 / 健康检查 / 请求追踪 / SSE 鉴权头化。"""
from __future__ import annotations

import base64
import logging
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def app_client(engine):
    from app.main import create_app
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _mk_user(session, username):
    from app.core.security import hash_password
    from app.db.models import Role, User
    u = User(username=username, password_hash=hash_password("pw123456"),
             display_name=username, role=Role.ADMIN)
    session.add(u)
    await session.commit()
    session._test_created.append(u)
    return u


def _make_token(user) -> str:
    from app.core.security import create_token
    return create_token(str(user.id), user.username, user.role.value)


class TestAlembicBaseline:
    def test_baseline_upgrade_creates_all_tables(self, tmp_path):
        """全新 SQLite 库跑 baseline upgrade 能建出与模型 metadata 一致的全量表。"""
        import sqlalchemy as sa
        from alembic import command
        from alembic.config import Config

        db_path = tmp_path / "alembic_new.db"
        cfg = Config(str(BACKEND_ROOT / "alembic" / "alembic.ini"))
        cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
        command.upgrade(cfg, "head")

        eng = sa.create_engine(f"sqlite:///{db_path}")
        try:
            tables = set(sa.inspect(eng).get_table_names())
        finally:
            eng.dispose()
        from app.db.models import Base
        assert set(Base.metadata.tables) <= tables
        # 基线已落版本号，老部署 stamp head 后等价
        assert "alembic_version" in tables

    def test_baseline_upgrade_idempotent_on_existing_db(self, tmp_path):
        """对已建表的老库重复 upgrade 不报错（checkfirst 跳过），等同 stamp 平滑接入。"""
        import sqlalchemy as sa
        from alembic import command
        from alembic.config import Config

        db_path = tmp_path / "alembic_old.db"
        # 先模拟老部署：直接 create_all 建表
        from app.db.models import Base
        eng = sa.create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(eng)
        eng.dispose()

        cfg = Config(str(BACKEND_ROOT / "alembic" / "alembic.ini"))
        cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
        command.upgrade(cfg, "head")  # 不抛错即通过

        eng = sa.create_engine(f"sqlite:///{db_path}")
        try:
            version = eng.connect().execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
        finally:
            eng.dispose()
        assert version == "0001_baseline"


class TestMigrateColumnsDialect:
    """_migrate_columns 的 BOOLEAN 默认值按方言生成（PG 下写 0 会静默失败的真 bug 修复）。"""

    def test_postgresql_uses_false_literal(self):
        from app.bootstrap import _column_additions
        bool_ddls = [ddl for _, _, ddl in _column_additions("postgresql")
                     if ddl.startswith("BOOLEAN")]
        assert bool_ddls, "应存在 BOOLEAN 补列项"
        assert all("DEFAULT false" in ddl for ddl in bool_ddls)

    def test_sqlite_and_mysql_use_zero(self):
        from app.bootstrap import _column_additions
        for dialect in ("sqlite", "mysql"):
            bool_ddls = [ddl for _, _, ddl in _column_additions(dialect)
                         if ddl.startswith("BOOLEAN")]
            assert all("DEFAULT 0" in ddl for ddl in bool_ddls), dialect

    def test_non_boolean_entries_dialect_invariant(self):
        from app.bootstrap import _column_additions
        pg = {(t, c): d for t, c, d in _column_additions("postgresql")}
        sq = {(t, c): d for t, c, d in _column_additions("sqlite")}
        assert set(pg) == set(sq)
        for key, ddl in pg.items():
            if not ddl.startswith("BOOLEAN"):
                assert sq[key] == ddl


class TestSettingsExternalized:
    def test_new_settings_defaults(self):
        from app.core.config import Settings
        s = Settings()
        assert s.app_env == "dev"
        assert s.app_tz == "Asia/Shanghai"
        assert s.alert_webhook_url == ""
        assert s.login_max_fails == 5
        assert s.login_lock_minutes == 10
        assert s.agent_max_steps == 8
        assert s.skill_python_timeout_sec == 30
        assert s.stale_scan_job_minutes == 30
        assert s.scheduler_catchup_hours == 24

    def test_consumers_read_from_settings(self):
        from app.agent import coding_agent
        from app.api.v1 import auth
        from app.core.config import settings
        from app.services import custom_skills
        assert auth.MAX_LOGIN_FAILS == settings.login_max_fails
        assert auth.LOCK_MINUTES == settings.login_lock_minutes
        assert custom_skills.PYTHON_TIMEOUT_SEC == settings.skill_python_timeout_sec
        assert coding_agent.MAX_STEPS == settings.agent_max_steps


class TestProductionConfigEnforcement:
    def test_production_rejects_default_jwt_secret(self):
        from app.core.config import Settings, validate_production_config
        good_key = base64.b64encode(b"k" * 32).decode()
        s = Settings(app_env="production", secret_encryption_key=good_key)
        with pytest.raises(RuntimeError, match="JWT_SECRET"):
            validate_production_config(s)

    def test_production_rejects_empty_encryption_key(self):
        from app.core.config import Settings, validate_production_config
        s = Settings(app_env="production", jwt_secret="prod-" + uuid.uuid4().hex,
                     secret_encryption_key="")
        with pytest.raises(RuntimeError, match="SECRET_ENCRYPTION_KEY"):
            validate_production_config(s)

    def test_production_lists_all_problems(self):
        from app.core.config import Settings, validate_production_config
        with pytest.raises(RuntimeError) as exc_info:
            validate_production_config(Settings(app_env="production"))
        msg = str(exc_info.value)
        assert "拒绝启动" in msg
        assert "JWT_SECRET" in msg and "SECRET_ENCRYPTION_KEY" in msg

    def test_production_compliant_passes(self):
        from app.core.config import Settings, validate_production_config
        s = Settings(app_env="production",
                     jwt_secret="prod-" + uuid.uuid4().hex,
                     secret_encryption_key=base64.b64encode(b"k" * 32).decode())
        validate_production_config(s)  # 不抛错即通过

    def test_non_production_warns_but_does_not_raise(self, caplog):
        from app.core.config import Settings, validate_production_config
        with caplog.at_level(logging.WARNING, logger="config"):
            validate_production_config(Settings(app_env="dev"))  # 默认密钥，仅告警
        assert "不合规" in caplog.text


class TestHealthAndReadyz:
    async def test_healthz_alias(self, app_client):
        r = await app_client.get("/api/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    async def test_readyz_ok_with_degraded_qdrant(self, app_client):
        """DB 正常 + Qdrant 不可达（测试环境指向 127.0.0.1:1）→ 200 且标注 degraded。

        Qdrant 是可选增强组件（语义记忆/向量索引），主链路有降级路径，不可达不判 503。
        """
        r = await app_client.get("/api/readyz")
        assert r.status_code == 200
        body = r.json()
        assert body["checks"]["db"] == "ok"
        assert body["checks"]["qdrant"].startswith("degraded")
        assert body["status"] == "degraded"

    async def test_readyz_503_when_db_down(self, app_client, monkeypatch):
        from sqlalchemy.ext.asyncio import create_async_engine
        bad_engine = create_async_engine("sqlite+aiosqlite:////nonexistent-dir-xyz/none.db")
        monkeypatch.setattr("app.db.session.get_engine", lambda: bad_engine)
        try:
            r = await app_client.get("/api/readyz")
        finally:
            await bad_engine.dispose()
        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "error"
        assert body["checks"]["db"].startswith("error")


class TestRequestId:
    async def test_response_carries_generated_request_id(self, app_client):
        r = await app_client.get("/api/health")
        assert r.status_code == 200
        assert r.headers.get("x-request-id")

    async def test_inbound_request_id_passthrough(self, app_client):
        r = await app_client.get("/api/health", headers={"X-Request-ID": "req-trace-001"})
        assert r.headers["x-request-id"] == "req-trace-001"

    async def test_request_id_in_log_record(self, app_client, caplog):
        from app.main import RequestIdFilter
        f = RequestIdFilter()
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "m", None, None)
        assert f.filter(record) is True
        assert record.request_id == "-"  # 无请求上下文时的占位


class TestSseAuthHeader:
    """SSE 鉴权头化：优先 Authorization 头，query token 过渡期兼容。"""

    async def test_authorization_header_authenticates(self, app_client, committed):
        u = await _mk_user(committed, f"sse-h-{uuid.uuid4().hex[:8]}")
        token = _make_token(u)
        r = await app_client.get("/api/v1/auth/me",
                                 headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["username"] == u.username

    async def test_query_token_still_authenticates(self, app_client, committed):
        """兼容期：query token（已废弃）仍可认证。"""
        u = await _mk_user(committed, f"sse-q-{uuid.uuid4().hex[:8]}")
        token = _make_token(u)
        r = await app_client.get(f"/api/v1/auth/me?token={token}")
        assert r.status_code == 200
        assert r.json()["username"] == u.username

    async def test_header_takes_priority_over_query(self, app_client, committed):
        """同时提供时以 header 为准：header 无效即使 query 有效也 401。"""
        u = await _mk_user(committed, f"sse-p-{uuid.uuid4().hex[:8]}")
        token = _make_token(u)
        r = await app_client.get(f"/api/v1/auth/me?token={token}",
                                 headers={"Authorization": "Bearer invalid-token"})
        assert r.status_code == 401
