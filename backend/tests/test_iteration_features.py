"""功能闭环迭代：LLM 切换策略接线 / Skill 版本回滚 / LLM 失败告警 / 探查留档 /
Azkaban 远端元数据搜索 / 库表答疑血缘简答 / 查询缓存 singleflight / 建议一键转任务。"""
from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import threading
import uuid
import zipfile
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import hash_password
from app.db.models import (AuditLog, Conversation, CustomSkill, DataSource, DbType, EtlTask,
                           LlmCallLog, LlmConfig, MetaColumn, MetaTable, ModelKind, Protocol,
                           Role, SkillStatus, SkillType, TableLayer, User)
from app.db.models.base import utcnow
from app.db.models.llm import CallStatus, LlmScene


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
    r = await client.post("/api/v1/auth/login",
                          json={"username": username, "password": "pw123456"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _mk_llm(session, name, *, policy="immediate", is_default=False):
    cfg = LlmConfig(name=name, protocol=Protocol.OPENAI, kind=ModelKind.CHAT,
                    endpoint="http://127.0.0.1:9/v1", api_key_cipher="",
                    model=f"m-{name}", timeout_sec=3, retry_count=0,
                    switch_policy=policy, is_default=is_default)
    session.add(cfg)
    session._test_created.append(cfg)
    return cfg


# ================= 1. LLM 切换策略「新会话生效」 =================

class TestSwitchPolicyNewSession:
    async def test_new_session_keeps_old_config(self, committed):
        from app.llm.gateway import gateway, invalidate_config_cache

        user = await _mk_user(committed, f"sp-{uuid.uuid4().hex[:6]}")
        cfg_a = _mk_llm(committed, f"spA-{uuid.uuid4().hex[:6]}",
                        policy="new_session", is_default=True)
        conv1 = Conversation(user_id=user.id, title="旧会话")
        committed.add(conv1)
        committed._test_created.append(conv1)
        await committed.commit()
        invalidate_config_cache()

        # 首次解析：走默认 A，并落快照
        r1 = await gateway.resolve_scene(LlmScene.CHAT_ANSWER,
                                         conversation_id=str(conv1.id))
        assert r1.config_id == str(cfg_a.id)
        await committed.refresh(conv1)
        assert str(conv1.llm_snapshot_config_id) == str(cfg_a.id)

        # 切换默认到 B（同为 new_session 策略）
        cfg_b = _mk_llm(committed, f"spB-{uuid.uuid4().hex[:6]}",
                        policy="new_session", is_default=True)
        cfg_a.is_default = False
        conv2 = Conversation(user_id=user.id, title="新会话")
        committed.add(conv2)
        committed._test_created.append(conv2)
        await committed.commit()
        invalidate_config_cache()

        # 旧会话继续走旧配置 A；新会话用新配置 B 并落快照
        r_old = await gateway.resolve_scene(LlmScene.CHAT_ANSWER,
                                            conversation_id=str(conv1.id))
        assert r_old.config_id == str(cfg_a.id)
        r_new = await gateway.resolve_scene(LlmScene.CHAT_ANSWER,
                                            conversation_id=str(conv2.id))
        assert r_new.config_id == str(cfg_b.id)
        await committed.refresh(conv2)
        assert str(conv2.llm_snapshot_config_id) == str(cfg_b.id)

    async def test_immediate_policy_takes_effect_at_once(self, committed):
        from app.llm.gateway import gateway, invalidate_config_cache

        user = await _mk_user(committed, f"spi-{uuid.uuid4().hex[:6]}")
        cfg_c = _mk_llm(committed, f"spC-{uuid.uuid4().hex[:6]}",
                        policy="immediate", is_default=True)
        conv = Conversation(user_id=user.id, title="即时策略会话")
        committed.add(conv)
        committed._test_created.append(conv)
        await committed.commit()
        invalidate_config_cache()

        r1 = await gateway.resolve_scene(LlmScene.CHAT_ANSWER,
                                         conversation_id=str(conv.id))
        assert r1.config_id == str(cfg_c.id)
        await committed.refresh(conv)
        assert conv.llm_snapshot_config_id is None  # immediate 策略不落快照

        # 换默认后旧会话立即跟随新配置
        cfg_d = _mk_llm(committed, f"spD-{uuid.uuid4().hex[:6]}",
                        policy="immediate", is_default=True)
        cfg_c.is_default = False
        await committed.commit()
        invalidate_config_cache()
        r2 = await gateway.resolve_scene(LlmScene.CHAT_ANSWER,
                                         conversation_id=str(conv.id))
        assert r2.config_id == str(cfg_d.id)


# ================= 2. Skill 版本回滚 + 调用审计摘要 =================

def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, c in files.items():
            z.writestr(n, c)
    return buf.getvalue()


def _skill_pkg(name: str, ver_mark: str) -> bytes:
    return _zip({
        "skill.yaml": (f"name: {name}\ndescription: 回滚测试\ntype: prompt\n"
                       f"trigger_keywords: [\"回滚测试{ver_mark}\"]\n"),
        "prompt.md": f"{ver_mark} 模板：${{question}}",
    })


class TestSkillRollback:
    async def test_rollback_to_v1(self, app_client, committed):
        admin = await _mk_user(committed, f"rb-{uuid.uuid4().hex[:6]}")
        h = await _login(app_client, admin.username)

        name = f"rb{uuid.uuid4().hex[:8]}"
        # 上传 v1 并启用
        r = await app_client.post("/api/v1/skills/upload", headers=h,
                                  files={"file": (f"{name}.zip", _skill_pkg(name, "v1"),
                                                  "application/zip")})
        assert r.status_code == 200, r.text
        v1 = r.json()
        assert v1["version"] == 1 and v1["status"] == "disabled"
        r = await app_client.post(f"/api/v1/skills/{v1['id']}/status",
                                  json={"status": "enabled"}, headers=h)
        assert r.json()["status"] == "enabled"

        # 上传 v2（自动停用 v1，自身停用）→ 启用 v2
        r = await app_client.post("/api/v1/skills/upload", headers=h,
                                  files={"file": (f"{name}.zip", _skill_pkg(name, "v2"),
                                                  "application/zip")})
        v2 = r.json()
        assert v2["version"] == 2
        r = await app_client.post(f"/api/v1/skills/{v2['id']}/status",
                                  json={"status": "enabled"}, headers=h)
        assert r.json()["status"] == "enabled"

        # 回滚到 v1：v1 启用、v2 停用、历史保留
        r = await app_client.post(f"/api/v1/skills/{v2['id']}/rollback",
                                  json={"target_version": 1}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["version"] == 1 and r.json()["status"] == "enabled"

        from sqlalchemy import select
        rows = (await committed.execute(
            select(CustomSkill).where(CustomSkill.name == name)
            .order_by(CustomSkill.version))).scalars().all()
        assert len(rows) == 2  # 历史版本全部保留
        enabled = [s for s in rows if s.status == SkillStatus.ENABLED]
        assert len(enabled) == 1 and enabled[0].version == 1  # 触发走 v1

        # 审计留档
        audits = (await committed.execute(
            select(AuditLog).where(AuditLog.action == "skill.rollback",
                                   AuditLog.resource_id == v1["id"]))).scalars().all()
        assert audits and audits[-1].detail["from_version"] == 2
        assert audits[-1].detail["target_version"] == 1

        # 也支持按历史版本行 id 回滚
        r = await app_client.post(f"/api/v1/skills/{v1['id']}/rollback",
                                  json={"target_skill_id": v2["id"]}, headers=h)
        assert r.status_code == 200 and r.json()["version"] == 2

    async def test_rollback_rejects_cross_name(self, app_client, committed):
        admin = await _mk_user(committed, f"rb2-{uuid.uuid4().hex[:6]}")
        h = await _login(app_client, admin.username)
        n1, n2 = f"rba{uuid.uuid4().hex[:6]}", f"rbb{uuid.uuid4().hex[:6]}"
        r1 = await app_client.post("/api/v1/skills/upload", headers=h,
                                   files={"file": ("a.zip", _skill_pkg(n1, "v1"),
                                                   "application/zip")})
        r2 = await app_client.post("/api/v1/skills/upload", headers=h,
                                   files={"file": ("b.zip", _skill_pkg(n2, "v1"),
                                                   "application/zip")})
        # 跨同名组的 target_skill_id 拒绝
        r = await app_client.post(f"/api/v1/skills/{r1.json()['id']}/rollback",
                                  json={"target_skill_id": r2.json()["id"]}, headers=h)
        assert r.status_code == 422


class _FakeCtx:
    """_run_custom_skill 的最小上下文（duck-typing PipelineContext）。"""

    def __init__(self, *, user_id, username, datasource, question):
        from app.agent.pipeline.context import StepTrace
        self._step_cls = StepTrace
        self.user_id, self.username = user_id, username
        self.conversation_id = str(uuid.uuid4())
        self.datasource = datasource
        self.question = question
        self.rewritten = question
        self.answer_text = ""
        self.duration_ms = 1
        self.llm_config_id = None
        self.steps: list = []

    def step(self, name, detail=""):
        st = self._step_cls(name, detail=detail)
        self.steps.append(st)
        return st

    def emit_step(self, st, detail=""):
        pass

    def emit(self, event, data):
        pass


class TestSkillInvokeAuditSummary:
    async def test_audit_contains_io_summary(self, committed):
        from app.agent.pipeline import orchestrator as orch

        user = await _mk_user(committed, f"sau-{uuid.uuid4().hex[:6]}")
        ds = DataSource(name=f"技能仓-{uuid.uuid4().hex[:8]}", db_type=DbType.SQLITE,
                        database_name=":memory:", username="")
        committed.add(ds)
        skill = CustomSkill(name=f"sau{uuid.uuid4().hex[:6]}", description="审计",
                            version=1, skill_type=SkillType.PROMPT,
                            trigger_keywords=["审计"], files={"prompt.md": "模板 ${question}"},
                            entry="prompt.md", status=SkillStatus.ENABLED,
                            created_by=user.id, created_by_name=user.username)
        committed.add(skill)
        committed._test_created.extend([ds, skill])
        await committed.commit()

        question = "审计问题" + "长" * 300  # 超 200 字，验证截断
        ctx = _FakeCtx(user_id=str(user.id), username=user.username,
                       datasource=ds, question=question)
        # 无可用 LLM → 走异常分支，但审计照常写入
        await orch._run_custom_skill(ctx, skill_row=skill, name=skill.name)

        from sqlalchemy import select
        audits = (await committed.execute(
            select(AuditLog).where(AuditLog.action == "skill.invoke",
                                   AuditLog.resource_id == str(skill.id)))).scalars().all()
        assert audits, "skill.invoke 审计应写入"
        detail = audits[-1].detail
        assert detail["question"] == question[:200]
        assert detail["output"] and len(detail["output"]) <= 200


# ================= 3. LLM 失败主动告警 + 近 15 分钟失败率 =================

class _WebhookSink:
    """本地 webhook 接收器：记录所有 POST 的 JSON body。"""

    def __init__(self):
        self.payloads: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode() if n else "{}"
                try:
                    outer.payloads.append(json.loads(raw))
                except json.JSONDecodeError:
                    outer.payloads.append({"_raw": raw})
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._srv.server_address[1]}/hook"

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=3)


class TestLlmFailureAlert:
    async def test_webhook_called_and_throttled(self, committed, monkeypatch):
        from app.core.config import settings
        from app.core.errors import LLMError
        from app.llm import gateway as gw
        from app.llm.types import ChatMessage

        sink = _WebhookSink()
        try:
            monkeypatch.setattr(settings, "alert_webhook_url", sink.url)
            cfg = _mk_llm(committed, f"alert-{uuid.uuid4().hex[:6]}", is_default=True)
            await committed.commit()
            gw.invalidate_config_cache()
            gw._ALERT_LAST_SENT.pop(str(cfg.id), None)

            # 端点不可达（127.0.0.1:9）→ 重试后仍失败 → 触发告警
            with pytest.raises(LLMError):
                await gw.gateway.chat(LlmScene.CHAT_ANSWER, [ChatMessage("user", "hi")])
            assert len(sink.payloads) == 1
            p = sink.payloads[0]
            assert p["config_name"] == cfg.name
            assert p["scene"] == "chat_answer"
            assert p["error"] and p["time"]

            # 节流：5 分钟内同配置不再发送
            with pytest.raises(LLMError):
                await gw.gateway.chat(LlmScene.CHAT_ANSWER, [ChatMessage("user", "hi2")])
            assert len(sink.payloads) == 1
        finally:
            sink.stop()

    async def test_webhook_failure_does_not_break_call(self, committed, monkeypatch):
        from app.core.config import settings
        from app.core.errors import LLMError
        from app.llm import gateway as gw
        from app.llm.types import ChatMessage

        # webhook 地址不可达：只记日志，主链路仍抛的是 LLMError 而非告警异常
        monkeypatch.setattr(settings, "alert_webhook_url", "http://127.0.0.1:9/hook")
        cfg = _mk_llm(committed, f"alertx-{uuid.uuid4().hex[:6]}", is_default=True)
        await committed.commit()
        gw.invalidate_config_cache()
        gw._ALERT_LAST_SENT.pop(str(cfg.id), None)
        with pytest.raises(LLMError):
            await gw.gateway.chat(LlmScene.CHAT_ANSWER, [ChatMessage("user", "hi")])

    async def test_no_webhook_configured_no_send(self, committed, monkeypatch):
        from app.core.config import settings
        from app.core.errors import LLMError
        from app.llm import gateway as gw
        from app.llm.types import ChatMessage

        monkeypatch.setattr(settings, "alert_webhook_url", "")
        cfg = _mk_llm(committed, f"alertn-{uuid.uuid4().hex[:6]}", is_default=True)
        await committed.commit()
        gw.invalidate_config_cache()
        gw._ALERT_LAST_SENT.pop(str(cfg.id), None)
        with pytest.raises(LLMError):
            await gw.gateway.chat(LlmScene.CHAT_ANSWER, [ChatMessage("user", "hi")])


class TestUsageFailureRate:
    async def test_recent_15m_fields(self, app_client, committed):
        admin = await _mk_user(committed, f"usg-{uuid.uuid4().hex[:6]}")
        h = await _login(app_client, admin.username)

        base = (await app_client.get("/api/v1/llm/usage", headers=h)).json()["recent_15m"]

        # 近 15 分钟：3 成功 + 1 失败；另 1 条 30 分钟前的失败不计入
        for _ in range(3):
            committed.add(LlmCallLog(scene="chat_answer", protocol="openai",
                                     model="m", status=CallStatus.OK))
        committed.add(LlmCallLog(scene="chat_answer", protocol="openai",
                                 model="m", status=CallStatus.ERROR, error_brief="x"))
        committed.add(LlmCallLog(scene="chat_answer", protocol="openai",
                                 model="m", status=CallStatus.ERROR, error_brief="old",
                                 created_at=utcnow() - timedelta(minutes=30)))
        await committed.commit()

        body = (await app_client.get("/api/v1/llm/usage", headers=h)).json()
        new = body["recent_15m"]
        assert new["calls"] - base["calls"] == 4
        assert new["errors"] - base["errors"] == 1
        assert 0.0 <= new["failure_rate"] <= 1.0
        assert "rows" in body and "group_by" in body  # 原结构保持


# ================= 4. 质量探查留档 =================

class TestProbeArchive:
    async def test_probe_archived_and_queryable(self, app_client, committed, tmp_path):
        admin = await _mk_user(committed, f"pb-{uuid.uuid4().hex[:6]}")
        analyst = await _mk_user(committed, f"pba-{uuid.uuid4().hex[:6]}", Role.ANALYST)
        db_path = tmp_path / "probe.db"
        conn = sqlite3.connect(db_path)
        conn.executescript("CREATE TABLE dwd_probe (id TEXT, val TEXT);")
        conn.executemany("INSERT INTO dwd_probe VALUES (?,?)",
                         [(f"k{i}", None if i % 5 else "v") for i in range(100)])
        conn.commit()
        conn.close()
        ds = DataSource(name=f"留档仓-{uuid.uuid4().hex[:8]}", db_type=DbType.SQLITE,
                        database_name=str(db_path), username="")
        committed.add(ds)
        await committed.flush()
        t = MetaTable(datasource_id=ds.id, table_name="dwd_probe", layer=TableLayer.DWD)
        committed.add(t)
        await committed.flush()
        committed.add(MetaColumn(table_id=t.id, column_name="id", data_type="text"))
        committed.add(MetaColumn(table_id=t.id, column_name="val", data_type="text"))
        committed._test_created.extend([ds, t])
        await committed.commit()

        ah = await _login(app_client, admin.username)
        uh = await _login(app_client, analyst.username)

        r = await app_client.post("/api/v1/governance/probe", headers=ah,
                                  json={"datasource_id": str(ds.id), "table": "dwd_probe"})
        assert r.status_code == 200, r.text
        run_id = r.json()["probe_run_id"]

        # 分页列表可查到留档
        r = await app_client.get("/api/v1/governance/probes", headers=ah)
        assert r.status_code == 200
        rows = r.json()["rows"]
        hit = next(x for x in rows if x["id"] == run_id)
        assert hit["table"] == "dwd_probe" and hit["username"] == admin.username
        assert "stats" not in hit  # 列表不带详情

        # 详情含 stats/findings/入参/耗时
        r = await app_client.get(f"/api/v1/governance/probes/{run_id}", headers=ah)
        assert r.status_code == 200
        detail = r.json()
        assert detail["stats"]["id"]["non_null"] > 0
        assert detail["params"]["sample_rows"] == 2000
        assert detail["duration_ms"] >= 0
        assert isinstance(detail["findings"], list)

        # 普通用户看不到他人的留档
        r = await app_client.get("/api/v1/governance/probes", headers=uh)
        assert all(x["id"] != run_id for x in r.json()["rows"])
        r = await app_client.get(f"/api/v1/governance/probes/{run_id}", headers=uh)
        assert r.status_code == 403


# ================= 5. 任务元数据搜索覆盖 Azkaban 远端 =================

class TestTaskMetadataAzkaban:
    async def test_keyword_filters_three_categories(self, app_client, committed, monkeypatch):
        admin = await _mk_user(committed, f"tm-{uuid.uuid4().hex[:6]}")
        analyst = await _mk_user(committed, f"tma-{uuid.uuid4().hex[:6]}", Role.ANALYST)
        ds = DataSource(name=f"搜索仓-{uuid.uuid4().hex[:8]}", db_type=DbType.MYSQL,
                        host="127.0.0.1", port=3306, database_name="d", username="r")
        committed.add(ds)
        await committed.flush()
        t = MetaTable(datasource_id=ds.id, table_name="dws_trade_daily",
                      comment="交易汇总", layer=TableLayer.DWS)
        committed.add(t)
        task = EtlTask(user_id=admin.id, name="trade 加工", target_table="dws_trade_daily",
                       layer="dws", etl_sql="SELECT 1")
        committed.add(task)
        committed._test_created.extend([ds, t, task])
        await committed.commit()

        async def fake_fetch(db):
            return [{"project": "etl", "flow": "dws_trade_daily"},
                    {"project": "etl", "flow": "ods_user_sync"}]

        monkeypatch.setattr("app.api.v1.modeling._fetch_azkaban_flows", fake_fetch)

        h = await _login(app_client, admin.username)
        r = await app_client.get("/api/v1/modeling/task-metadata?q=trade", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert any("trade" in x["name"] for x in body["tasks"])
        assert any("trade" in x["name"] for x in body["tables"])
        assert [f["flow"] for f in body["azkaban_flows"]] == ["dws_trade_daily"]

        # 无关键词：不打远端，azkaban_flows 为空但字段存在（向后兼容）
        r = await app_client.get("/api/v1/modeling/task-metadata", headers=h)
        assert r.json()["azkaban_flows"] == []

        # 非管理员：不返回远端 flow 类别内容
        uh = await _login(app_client, analyst.username)
        r = await app_client.get("/api/v1/modeling/task-metadata?q=trade", headers=uh)
        assert r.json()["azkaban_flows"] == []

    async def test_remote_failure_gracefully_skipped(self, app_client, committed, monkeypatch):
        admin = await _mk_user(committed, f"tm2-{uuid.uuid4().hex[:6]}")
        h = await _login(app_client, admin.username)

        async def boom(db):
            raise RuntimeError("azkaban down")

        # _fetch 内部兜底；这里模拟连 _fetch 本身都炸的极端情况也不应影响本地搜索
        monkeypatch.setattr("app.api.v1.modeling._fetch_azkaban_flows", boom)
        r = await app_client.get("/api/v1/modeling/task-metadata?q=trade", headers=h)
        assert r.status_code == 200
        assert r.json()["azkaban_flows"] == []


# ================= 6. 库表答疑血缘简答 =================

class TestTableQaLineage:
    async def test_lineage_candidates_inferred(self, session):
        from app.agent.skills import table_qa

        ds = DataSource(name=f"血缘仓-{uuid.uuid4().hex[:8]}", db_type=DbType.MYSQL,
                        host="127.0.0.1", port=3306, database_name="d", username="r")
        session.add(ds)
        await session.flush()
        for name, layer in [("dws_trade_daily", TableLayer.DWS),
                            ("dwd_trade_detail", TableLayer.DWD),
                            ("ods_trade_raw", TableLayer.ODS),
                            ("dws_user_daily", TableLayer.DWS)]:
            session.add(MetaTable(datasource_id=ds.id, table_name=name, layer=layer))
        await session.commit()

        ctx, matched, _ = await table_qa.build_context(session, ds.id,
                                                       "dws_trade_daily 是干嘛的")
        assert matched == ["dws_trade_daily"]
        assert "血缘候选" in ctx
        assert "dwd_trade_detail" in ctx and "ods_trade_raw" in ctx
        assert "按命名推断" in ctx
        # 同层表不应成为上游候选（dws_user_daily 只可能出现在其它表清单）
        lineage_section = ctx.split("血缘候选", 1)[1]
        assert "dws_user_daily" not in lineage_section.split("##", 1)[0]

    async def test_no_lineage_for_dim_table(self, session):
        from app.agent.skills import table_qa

        ds = DataSource(name=f"血缘仓2-{uuid.uuid4().hex[:8]}", db_type=DbType.MYSQL,
                        host="127.0.0.1", port=3306, database_name="d", username="r")
        session.add(ds)
        await session.flush()
        session.add(MetaTable(datasource_id=ds.id, table_name="dim_channel",
                              layer=TableLayer.DIM, comment="渠道"))
        await session.commit()
        ctx, matched, _ = await table_qa.build_context(session, ds.id,
                                                       "dim_channel 是干嘛的")
        assert matched == ["dim_channel"]
        assert "血缘候选" not in ctx  # DIM 无上游层映射


# ================= 7. 查询缓存 singleflight =================

class TestSingleflight:
    async def test_concurrent_same_key_runs_once(self):
        from app.agent.pipeline import orchestrator as orch

        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)  # 模拟数仓查询耗时
            return {"rows": 1}

        key = f"sfk-{uuid.uuid4().hex[:6]}"
        results = await asyncio.gather(
            *[orch._run_singleflight(key, factory) for _ in range(5)])
        assert calls == 1, "并发 5 个同 key 请求只应执行 1 次数仓查询"
        assert all(r[0] == {"rows": 1} for r in results)
        assert sum(1 for r in results if r[1]) == 1  # 恰好一个执行者

    async def test_exception_propagates_and_registry_cleaned(self):
        from app.agent.pipeline import orchestrator as orch

        key = f"sfk-{uuid.uuid4().hex[:6]}"

        async def boom():
            await asyncio.sleep(0.01)
            raise ValueError("查询失败")

        outs = await asyncio.gather(
            *[orch._run_singleflight(key, boom) for _ in range(3)],
            return_exceptions=True)
        assert all(isinstance(o, ValueError) for o in outs)
        assert key not in orch._INFLIGHT_QUERIES  # 失败后清理，后续可重新竞争


# ================= 8. 建模规划建议一键转任务 =================

class TestAdviceSuggestedTask:
    async def test_findings_carry_suggested_task(self, app_client, committed):
        admin = await _mk_user(committed, f"adv-{uuid.uuid4().hex[:6]}")
        ds = DataSource(name=f"建议仓-{uuid.uuid4().hex[:8]}", db_type=DbType.MYSQL,
                        host="127.0.0.1", port=3306, database_name="d", username="r")
        committed.add(ds)
        await committed.flush()
        t = MetaTable(datasource_id=ds.id, table_name="ads_orphan_report",
                      comment="孤儿报表", layer=TableLayer.ADS)
        committed.add(t)
        committed._test_created.extend([ds, t])
        await committed.commit()

        h = await _login(app_client, admin.username)
        r = await app_client.post("/api/v1/modeling/advice", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["findings"]
        # 每条 finding 都带 suggested_task 键（可为 None，向后兼容）
        assert all("suggested_task" in f for f in body["findings"])

        no_task = next(f for f in body["findings"] if f["title"] == "汇总表无调度任务")
        st = no_task["suggested_task"]
        assert st is not None
        assert st["target_table"] in body["overview"]["no_task_tables"]
        assert st["name"] and st["reason"]
        assert st["layer"] in ("dws", "ads", "")
