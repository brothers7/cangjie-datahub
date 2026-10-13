"""鉴权与数据源权限：KG 只读接口登录校验、数据源隔离、脱敏规则 CRUD。"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import hash_password
from app.db.models import (DataSource, DatasourcePermission, DbType, Role, User)


@pytest.fixture
async def app_client(engine):
    """真实 FastAPI 应用的异步 HTTP 客户端（隔离测试库）。"""
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _mk_user(session, username, role=Role.VIEWER):
    u = User(username=username, password_hash=hash_password("pw123456"),
             display_name=username, role=role)
    session.add(u)
    await session.commit()
    session._test_created.append(u)
    return u


async def _mk_ds(session, name):
    ds = DataSource(name=name, db_type=DbType.SQLITE, database_name="./x.db",
                    password_cipher="", username="")
    session.add(ds)
    await session.commit()
    session._test_created.append(ds)
    return ds


async def _login(client, username, password="pw123456"):
    r = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


class TestKgAuth:
    """KG 只读接口必须登录（审计发现此前匿名可读）。"""

    async def test_kg_endpoints_require_login(self, app_client, session):
        for path in ("/api/v1/kg/graph", "/api/v1/kg/metrics",
                     "/api/v1/kg/dimensions", "/api/v1/kg/rules", "/api/v1/kg/validation/report"):
            r = await app_client.get(path)
            assert r.status_code == 401, f"{path} 未鉴权"

    async def test_kg_metrics_accessible_after_login(self, app_client, committed):
        session = committed
        await _mk_user(session, "kg_reader")
        token = await _login(app_client, "kg_reader")
        r = await app_client.get("/api/v1/kg/metrics",
                                 headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200


class TestDatasourceIsolation:
    """非 admin 只能看到被授权的数据源；越权查询被拒绝。"""

    async def test_list_datasources_filters_by_permission(self, app_client, committed):
        session = committed
        admin = await _mk_user(session, "ds_admin", role=Role.ADMIN)
        viewer = await _mk_user(session, "ds_viewer")
        ds1 = await _mk_ds(session, "库A")
        await _mk_ds(session, "库B")

        admin_tok = await _login(app_client, "ds_admin")
        viewer_tok = await _login(app_client, "ds_viewer")

        # admin 看到全部
        r = await app_client.get("/api/v1/datasources",
                                 headers={"Authorization": f"Bearer {admin_tok}"})
        assert r.status_code == 200 and len(r.json()) == 2

        # viewer 未授权时看不到任何数据源
        r = await app_client.get("/api/v1/datasources",
                                 headers={"Authorization": f"Bearer {viewer_tok}"})
        assert r.status_code == 200 and r.json() == []

        # 授权 库A 后只能看到 库A
        perm = DatasourcePermission(user_id=viewer.id, datasource_id=ds1.id, can_query=True)
        session.add(perm)
        await session.commit()
        session._test_created.append(perm)
        r = await app_client.get("/api/v1/datasources",
                                 headers={"Authorization": f"Bearer {viewer_tok}"})
        assert [d["name"] for d in r.json()] == ["库A"]

    async def test_conversation_denied_without_permission(self, app_client, committed):
        session = committed
        viewer = await _mk_user(session, "conv_viewer")
        ds = await _mk_ds(session, "受限库")
        tok = await _login(app_client, "conv_viewer")
        r = await app_client.post("/api/v1/chat/conversations",
                                  json={"datasource_id": str(ds.id)},
                                  headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403

    async def test_user_management_requires_admin(self, app_client, committed):
        session = committed
        await _mk_user(session, "plain_user")
        tok = await _login(app_client, "plain_user")
        h = {"Authorization": f"Bearer {tok}"}
        r = await app_client.get("/api/v1/users", headers=h)
        assert r.status_code == 403, "GET /users 未拦非管理员"
        r = await app_client.post("/api/v1/users", headers=h,
                                  json={"username": "x", "password": "y123456"})
        assert r.status_code == 403, "POST /users 未拦非管理员"


class TestMaskingRules:
    """脱敏规则 CRUD（审计发现此前只能直接插库）。"""

    async def test_crud_and_admin_only(self, app_client, committed):
        session = committed
        await _mk_user(session, "mask_admin", role=Role.ADMIN)
        viewer = await _mk_user(session, "mask_viewer")
        ds = await _mk_ds(session, "脱敏库")
        admin_tok = await _login(app_client, "mask_admin")
        viewer_tok = await _login(app_client, "mask_viewer")
        url = f"/api/v1/datasources/{ds.id}/masking-rules"

        # 非 admin 禁止访问
        r = await app_client.get(url, headers={"Authorization": f"Bearer {viewer_tok}"})
        assert r.status_code == 403

        # 创建
        r = await app_client.post(url, json={"column_pattern": "*phone*", "strategy": "mask_partial"},
                                  headers={"Authorization": f"Bearer {admin_tok}"})
        assert r.status_code == 200, r.text
        rid = r.json()["id"]

        # 非法策略拒绝
        r = await app_client.post(url, json={"column_pattern": "x", "strategy": "bogus"},
                                  headers={"Authorization": f"Bearer {admin_tok}"})
        assert r.status_code in (400, 422)

        # 列表
        r = await app_client.get(url, headers={"Authorization": f"Bearer {admin_tok}"})
        assert [x["column_pattern"] for x in r.json()] == ["*phone*"]

        # 更新（停用）
        r = await app_client.patch(f"/api/v1/datasources/masking-rules/{rid}",
                                   json={"enabled": False},
                                   headers={"Authorization": f"Bearer {admin_tok}"})
        assert r.status_code == 200 and r.json()["enabled"] is False

        # 删除
        r = await app_client.delete(f"/api/v1/datasources/masking-rules/{rid}",
                                    headers={"Authorization": f"Bearer {admin_tok}"})
        assert r.status_code == 200
        r = await app_client.get(url, headers={"Authorization": f"Bearer {admin_tok}"})
        assert r.json() == []
