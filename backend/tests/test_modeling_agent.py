"""PRD 3.3D-1/3/6：AI 建模工程师（内置编码代理引擎）与建模建议会话。"""
from __future__ import annotations

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.agent import coding_agent
from app.core.security import hash_password
from app.db.models import (DataSource, DbType, MetaColumn, MetaTable, ModelingMessage,
                           ModelingSession, Role, TableLayer, User)
from app.llm.types import ChatMessage, LLMResponse


class _ScriptedGateway:
    """按脚本依次返回 LLM 响应的假网关（记录收到的消息）。"""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[list[ChatMessage]] = []

    async def chat(self, scene, messages, **kw):
        self.calls.append(list(messages))
        if not self.replies:
            raise RuntimeError("脚本耗尽")
        return LLMResponse(text=self.replies.pop(0), model="fake")


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


async def _login(client, username):
    r = await client.post("/api/v1/auth/login", json={"username": username, "password": "pw123456"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def _mk_ds_meta(session):
    ds = DataSource(name=f"代理仓-{uuid.uuid4().hex[:8]}", db_type=DbType.MYSQL,
                    host="127.0.0.1", port=3306, database_name="d", username="r")
    session.add(ds)
    await session.flush()
    t = MetaTable(datasource_id=ds.id, table_name="dwd_trade", comment="交易明细表",
                  layer=TableLayer.DWD, partition_cols=["dt"])
    session.add(t)
    await session.flush()
    for col in ("dt", "channel_id", "gmv", "order_cnt"):
        session.add(MetaColumn(table_id=t.id, column_name=col, data_type="string"))
    await session.commit()
    return ds


class FakeCtx:
    def __init__(self):
        self.steps = []

    def step(self, name, detail=""):
        from app.agent.pipeline.context import StepTrace
        st = StepTrace(name, detail=detail)
        self.steps.append(st)
        return st

    def emit_step(self, st, detail=""):
        pass

    def emit(self, event, data):
        pass


def _scope(session, ds):
    return coding_agent.AgentScope(db=session, datasource_id=ds.id, dialect="mysql")


class TestAgentLoop:
    async def test_tool_call_then_final(self, session, monkeypatch):
        """工具调用 → 回喂 → final 产出，产出过安全剔除与校验。"""
        ds = await _mk_ds_meta(session)
        fake = _ScriptedGateway([
            json.dumps({"action": "tool_call", "tool": "search_tables",
                        "args": {"keyword": "交易"}, "thought": "先找交易表"}),
            json.dumps({"action": "tool_call", "tool": "get_table_schema",
                        "args": {"table": "dwd_trade"}}),
            json.dumps({"action": "final", "summary": "建一张日汇总表",
                        "tables": [{"name": "dws_trade_daily", "layer": "dws",
                                    "comment": "交易日汇总", "grain": "天",
                                    "ddl": "CREATE TABLE IF NOT EXISTS dws_trade_daily "
                                           "(dt string COMMENT '日期', gmv double COMMENT 'GMV')",
                                    "etl_sql": "INSERT OVERWRITE TABLE dws_trade_daily "
                                               "PARTITION(dt='${ds}') "
                                               "SELECT dt, SUM(gmv) FROM dwd_trade GROUP BY dt",
                                    "schedule": "每日", "cron": "", "depends_on": [],
                                    "source_tables": ["dwd_trade"], "rationale": "聚合明细"}],
                        "reuse_notes": "", "risks": []}),
        ])
        monkeypatch.setattr(coding_agent.gateway, "chat", fake.chat)
        ctx = FakeCtx()
        result = await coding_agent.run_agent(
            coding_agent.MODE_PROPOSE, "按天汇总交易", _scope(session, ds), ctx=ctx)
        assert result.error == ""
        assert result.payload and result.payload["tables"][0]["name"] == "dws_trade_daily"
        assert result.steps_used == 3
        # 工具结果回喂进了上下文
        assert any("TOOL_RESULT" in m.content for m in fake.calls[-1][1:])
        # 步骤可见
        details = [s.detail for s in ctx.steps]
        assert any("search_tables" in d for d in details)

    async def test_dangerous_sql_stripped_in_final(self, session, monkeypatch):
        """final 产出里的 DROP 语句被安全剔除并告警。"""
        ds = await _mk_ds_meta(session)
        fake = _ScriptedGateway([
            json.dumps({"action": "final", "summary": "s",
                        "tables": [{"name": "dws_a", "layer": "dws", "comment": "c",
                                    "ddl": "DROP TABLE IF EXISTS dws_a; CREATE TABLE dws_a (dt string)",
                                    "etl_sql": "INSERT OVERWRITE TABLE dws_a SELECT dt FROM dwd_trade",
                                    "source_tables": ["dwd_trade"]}]}),
        ])
        monkeypatch.setattr(coding_agent.gateway, "chat", fake.chat)
        result = await coding_agent.run_agent(
            coding_agent.MODE_PROPOSE, "x", _scope(session, ds))
        assert result.payload
        assert "DROP" not in result.payload["tables"][0]["ddl"].upper()
        assert any("危险语句" in w for w in result.warnings)

    async def test_max_steps_cap(self, session, monkeypatch):
        """代理死循环调用工具时，步数上限强制收尾。"""
        ds = await _mk_ds_meta(session)
        fake = _ScriptedGateway([
            json.dumps({"action": "tool_call", "tool": "search_tables",
                        "args": {"keyword": "x"}})
        ] * coding_agent.MAX_STEPS)
        monkeypatch.setattr(coding_agent.gateway, "chat", fake.chat)
        result = await coding_agent.run_agent(
            coding_agent.MODE_PROPOSE, "x", _scope(session, ds))
        assert result.payload is None
        assert "步数上限" in result.error

    async def test_unknown_tool_and_bad_json_recover(self, session, monkeypatch):
        """未知工具/非 JSON 输出不致命，回喂后代理可恢复。"""
        ds = await _mk_ds_meta(session)
        fake = _ScriptedGateway([
            "这不是 JSON",
            json.dumps({"action": "tool_call", "tool": "drop_table", "args": {}}),
            json.dumps({"action": "final", "ddl": "CREATE TABLE dws_a (dt string)",
                        "etl_sql": "INSERT OVERWRITE TABLE dws_a SELECT dt FROM dwd_trade",
                        "change_summary": "调整了字段"}),
        ])
        monkeypatch.setattr(coding_agent.gateway, "chat", fake.chat)
        result = await coding_agent.run_agent(
            coding_agent.MODE_REVISE, "把 dt 改成字符串", _scope(session, ds))
        assert result.payload and result.payload["change_summary"] == "调整了字段"
        last = fake.calls[-1]
        assert any("未知工具" in m.content for m in last if m.role == "user")

    async def test_llm_down_degrades(self, session, monkeypatch):
        async def boom(scene, messages, **kw):
            raise RuntimeError("LLM down")
        monkeypatch.setattr(coding_agent.gateway, "chat", boom)
        ds = await _mk_ds_meta(session)
        result = await coding_agent.run_agent(
            coding_agent.MODE_PROPOSE, "x", _scope(session, ds))
        assert result.payload is None and "LLM" in result.error


class TestModelingSessionApi:
    async def test_session_crud_and_degrade_message(self, app_client, committed):
        """创建会话 → SSE 发消息（LLM 不可用降级）→ 消息落库可查。"""
        await _mk_user(committed, "model_s1")
        h = await _login(app_client, "model_s1")
        r = await app_client.post("/api/v1/modeling/sessions",
                                  json={"requirement": "我要一张渠道日汇总表"}, headers=h)
        assert r.status_code == 200, r.text
        sid = r.json()["id"]

        # SSE 消息（默认 LLM 不可用 → 降级但流程完整）
        async with app_client.stream("POST", f"/api/v1/modeling/sessions/{sid}/messages",
                                     json={"content": "我要一张渠道日汇总表"}, headers=h) as resp:
            assert resp.status_code == 200
            body = ""
            async for chunk in resp.aiter_text():
                body += chunk
        assert "event: done" in body

        detail = (await app_client.get(f"/api/v1/modeling/sessions/{sid}", headers=h)).json()
        assert len(detail["messages"]) == 2
        assert detail["messages"][0]["role"] == "user"
        assert detail["messages"][1]["role"] == "assistant"
        assert detail["messages"][1]["proposal"] is None  # 降级无方案

        # 会话列表
        rows = (await app_client.get("/api/v1/modeling/sessions", headers=h)).json()
        assert any(x["id"] == sid and x["message_count"] == 2 for x in rows)

    async def test_accept_creates_tasks_and_dedup(self, app_client, committed):
        """接受方案 → 批量创建任务草稿（来源 ai，关联会话）；重复接受不重复建。"""
        u = await _mk_user(committed, "model_s2")
        h = await _login(app_client, "model_s2")
        sess = ModelingSession(user_id=u.id, title="t", requirement="r")
        committed.add(sess)
        await committed.flush()
        proposal = {"summary": "方案", "risks": [], "tables": [
            {"name": "dws_trade_daily", "layer": "dws", "comment": "交易日汇总",
             "ddl": "CREATE TABLE IF NOT EXISTS dws_trade_daily (dt string)",
             "etl_sql": "INSERT OVERWRITE TABLE dws_trade_daily SELECT dt FROM dwd_trade",
             "schedule": "每日", "cron": "0 30 2 * * ?", "depends_on": ["dwd_trade_sync"],
             "source_tables": ["dwd_trade"], "rationale": "聚合"},
            {"name": "dws_channel_daily", "layer": "dws", "comment": "渠道日汇总",
             "ddl": "CREATE TABLE IF NOT EXISTS dws_channel_daily (dt string)",
             "etl_sql": "INSERT OVERWRITE TABLE dws_channel_daily SELECT dt FROM dwd_trade",
             "source_tables": ["dwd_trade"]},
        ]}
        committed.add(ModelingMessage(session_id=sess.id, seq=0, role="user", content="需求"))
        m = ModelingMessage(session_id=sess.id, seq=1, role="assistant",
                            content="方案", proposal=proposal)
        committed.add(m)
        await committed.commit()

        r = await app_client.post(f"/api/v1/modeling/sessions/{sess.id}/accept",
                                  json={"message_id": str(m.id)}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["created"] == 2
        assert all(x["ok"] for x in r.json()["results"])

        tasks = (await app_client.get("/api/v1/etl/tasks", headers=h)).json()
        mine = [t for t in tasks if t["target_table"] in
                ("dws_trade_daily", "dws_channel_daily")]
        assert len(mine) == 2
        assert all(t["source"] == "ai" and t["status"] == "draft" for t in mine)
        assert mine[0]["warnings"] == [] or isinstance(mine[0]["warnings"], list)

        # 重复接受：不重复建
        r2 = await app_client.post(f"/api/v1/modeling/sessions/{sess.id}/accept",
                                   json={"message_id": str(m.id)}, headers=h)
        assert r2.json()["created"] == 0

        # 会话状态已落地
        detail = (await app_client.get(f"/api/v1/modeling/sessions/{sess.id}", headers=h)).json()
        assert detail["status"] == "accepted"

        # 权限隔离：别人不能看这个会话
        await _mk_user(committed, "model_s3")
        h3 = await _login(app_client, "model_s3")
        r3 = await app_client.get(f"/api/v1/modeling/sessions/{sess.id}", headers=h3)
        assert r3.status_code == 403

    async def test_ai_revise_sse_degrade(self, app_client, committed):
        """任务 AI 修订接口：LLM 降级时 SSE 完整结束（revision 为空）。"""
        await _mk_user(committed, "model_s4")
        h = await _login(app_client, "model_s4")
        r = await app_client.post("/api/v1/etl/tasks", json={
            "name": "修订测试", "target_table": "dws_a",
            "ddl": "CREATE TABLE dws_a (dt string)",
            "etl_sql": "INSERT OVERWRITE TABLE dws_a SELECT 1"}, headers=h)
        tid = r.json()["id"]
        async with app_client.stream("POST", f"/api/v1/etl/tasks/{tid}/ai-revise",
                                     json={"instruction": "加上渠道维度"}, headers=h) as resp:
            assert resp.status_code == 200
            body = ""
            async for chunk in resp.aiter_text():
                body += chunk
        assert "event: revision" in body and "event: done" in body
        await app_client.delete(f"/api/v1/etl/tasks/{tid}", headers=h)
