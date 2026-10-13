"""会话级模型覆盖：绑定 chat 配置、拒绝 embedding 配置、管道透传 override。"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import hash_password
from app.db.models import LlmConfig, ModelKind, Protocol, Role, User


@pytest.fixture
async def app_client(engine):
    from app.main import create_app
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _mk_user(session, username, role=Role.ANALYST):
    u = User(username=username, password_hash=hash_password("pw123456"),
             display_name=username, role=role)
    session.add(u)
    await session.commit()
    session._test_created.append(u)
    return u


async def _mk_llm(session, name, kind=ModelKind.CHAT):
    cfg = LlmConfig(name=name, protocol=Protocol.OPENAI, kind=kind,
                    endpoint="http://127.0.0.1:1/v1", api_key_cipher="", model="test-model")
    session.add(cfg)
    await session.commit()
    session._test_created.append(cfg)
    return cfg


async def _login(client, username, password="pw123456"):
    r = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


class TestChatOptions:
    async def test_chat_options_login_required_and_chat_only(self, app_client, committed):
        await _mk_user(committed, "opt_user")
        await _mk_llm(committed, "gpt4o", ModelKind.CHAT)
        await _mk_llm(committed, "emb-model", ModelKind.EMBEDDING)

        # 未登录被拒
        r = await app_client.get("/api/v1/llm/chat-options")
        assert r.status_code == 401

        tok = await _login(app_client, "opt_user")
        r = await app_client.get("/api/v1/llm/chat-options",
                                 headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        names = [o["name"] for o in r.json()]
        assert "gpt4o" in names and "emb-model" not in names  # 只列对话模型


class TestConversationLlmBinding:
    async def test_create_and_switch(self, app_client, committed):
        await _mk_user(committed, "bind_user")
        cfg = await _mk_llm(committed, "claude-sonnet")
        tok = await _login(app_client, "bind_user")
        h = {"Authorization": f"Bearer {tok}"}

        # 创建时绑定
        r = await app_client.post("/api/v1/chat/conversations",
                                  json={"title": "带模型", "llm_config_id": str(cfg.id)}, headers=h)
        assert r.status_code == 200, r.text
        cid = r.json()["id"]

        convs = (await app_client.get("/api/v1/chat/conversations", headers=h)).json()
        assert convs[0]["llm_config_id"] == str(cfg.id)

        # 切换为默认（清除覆盖）
        r = await app_client.patch(f"/api/v1/chat/conversations/{cid}",
                                   json={"llm_config_id": None}, headers=h)
        assert r.status_code == 200
        convs = (await app_client.get("/api/v1/chat/conversations", headers=h)).json()
        assert convs[0]["llm_config_id"] is None

    async def test_reject_embedding_config(self, app_client, committed):
        await _mk_user(committed, "bind_user2")
        emb = await _mk_llm(committed, "emb-only", ModelKind.EMBEDDING)
        tok = await _login(app_client, "bind_user2")
        r = await app_client.post("/api/v1/chat/conversations",
                                  json={"llm_config_id": str(emb.id)},
                                  headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code in (400, 422)

    async def test_reject_nonexistent_config(self, app_client, committed):
        await _mk_user(committed, "bind_user3")
        tok = await _login(app_client, "bind_user3")
        r = await app_client.post("/api/v1/chat/conversations",
                                  json={"llm_config_id": str(uuid.uuid4())},
                                  headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code in (400, 404, 422)


class TestGatewayOverride:
    """网关层：override_config_id 优先于场景绑定/全局默认。"""

    async def test_override_takes_precedence(self, committed):
        from app.llm.gateway import gateway, invalidate_config_cache
        from app.db.models.llm import LlmScene

        default = await _mk_llm(committed, "gw-default", ModelKind.CHAT)
        default.is_default = True
        override = await _mk_llm(committed, "gw-override", ModelKind.CHAT)
        await committed.commit()
        invalidate_config_cache()

        r1 = await gateway.resolve_scene(LlmScene.SQL_GENERATE)
        assert r1.config_id == str(default.id)          # 默认

        r2 = await gateway.resolve_scene(LlmScene.SQL_GENERATE,
                                         override_config_id=str(override.id))
        assert r2.config_id == str(override.id)         # 会话覆盖优先

    async def test_override_kind_mismatch_ignored(self, committed):
        from app.llm.gateway import gateway, invalidate_config_cache
        from app.db.models.llm import LlmScene

        default = await _mk_llm(committed, "gw2-default", ModelKind.CHAT)
        default.is_default = True
        emb = await _mk_llm(committed, "gw2-emb", ModelKind.EMBEDDING)
        await committed.commit()
        invalidate_config_cache()

        # 给 chat 场景传 embedding 配置 → 忽略覆盖，回落默认 chat
        r = await gateway.resolve_scene(LlmScene.SQL_GENERATE, override_config_id=str(emb.id))
        assert r.config_id == str(default.id)
