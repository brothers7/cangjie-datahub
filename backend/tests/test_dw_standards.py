"""PRD 3.3E：数仓规范（CRUD 权限 + 结构化规则校验 + 编码代理合规）。"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.agent import coding_agent
from app.core.security import hash_password
from app.db.models import (DataSource, DbType, DwStandard, MetaColumn, MetaTable, Role,
                           TableLayer, User)
from app.llm.types import LLMResponse
from app.services import dw_standards


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


def _std(name="分层命名规范", rules=None, enabled=True):
    return DwStandard(name=name, content="## 命名\n表名必须带分层前缀。",
                      rules=rules if rules is not None else ["layer_prefix_required"],
                      enabled=enabled, updated_by="admin")


class TestRuleChecks:
    def test_layer_prefix(self):
        s = _std()
        assert dw_standards.check_rules(s and [s], table_name="trade_sum", layer="dws")
        assert not dw_standards.check_rules([s], table_name="dws_trade_sum", layer="dws")

    def test_partition_and_comment(self):
        s = _std(rules=["partition_required", "comment_required", "partition_filter_required"])
        v = dw_standards.check_rules(
            [s], table_name="dws_a", layer="dws",
            ddl="CREATE TABLE IF NOT EXISTS dws_a (x string)",
            etl_sql="INSERT OVERWRITE TABLE dws_a SELECT 1")
        assert len(v) == 3   # 无分区、无中文注释、无分区过滤
        ok = dw_standards.check_rules(
            [s], table_name="dws_a", layer="dws",
            ddl="CREATE TABLE IF NOT EXISTS dws_a (x string) COMMENT '汇总表' "
                "PARTITIONED BY (ds string)",
            etl_sql="INSERT OVERWRITE TABLE dws_a PARTITION(ds='${ds}') SELECT 1 WHERE ds='${ds}'")
        assert not ok

    def test_reverse_layer_ref(self):
        s = _std(rules=["no_reverse_layer_ref"])
        v = dw_standards.check_rules([s], table_name="dwd_a", layer="dwd",
                                     source_layers={"ads_x": "ads"})
        assert v and "反向引用" in v[0]
        assert not dw_standards.check_rules([s], table_name="dwd_a", layer="dwd",
                                            source_layers={"ods_x": "ods"})

    def test_disabled_or_no_rules_noop(self):
        assert not dw_standards.check_rules([_std(rules=[])], table_name="x", ddl="CREATE TABLE x")
        assert not dw_standards.check_rules([], table_name="x", ddl="CREATE TABLE x")


class TestStandardsApi:
    async def test_crud_and_permission(self, app_client, committed):
        admin = await _mk_user(committed, f"adm-{uuid.uuid4().hex[:6]}", Role.ADMIN)
        analyst = await _mk_user(committed, f"ana-{uuid.uuid4().hex[:6]}")
        ah = await _login(app_client, admin.username)
        uh = await _login(app_client, analyst.username)

        # 规则字典可查
        r = await app_client.get("/api/v1/standards/rule-defs", headers=uh)
        assert r.status_code == 200 and len(r.json()["rules"]) == 5

        # 非管理员不可写
        r = await app_client.post("/api/v1/standards", headers=uh,
                                  json={"name": "X", "content": "c"})
        assert r.status_code == 403

        # 管理员 CRUD
        r = await app_client.post("/api/v1/standards", headers=ah,
                                  json={"name": "分层命名规范", "content": "正文",
                                        "rules": ["layer_prefix_required"]})
        assert r.status_code == 200, r.text
        sid = r.json()["id"]
        r = await app_client.patch(f"/api/v1/standards/{sid}", headers=ah,
                                   json={"name": "分层命名规范", "content": "正文v2",
                                         "rules": ["layer_prefix_required", "comment_required"],
                                         "enabled": False})
        assert r.json()["enabled"] is False and len(r.json()["rules"]) == 2
        # 未知规则开关被拒
        r = await app_client.patch(f"/api/v1/standards/{sid}", headers=ah,
                                   json={"name": "x", "rules": ["nope"]})
        assert r.status_code == 422
        # 登录用户可读列表
        r = await app_client.get("/api/v1/standards", headers=uh)
        assert r.status_code == 200 and len(r.json()["standards"]) == 1
        r = await app_client.delete(f"/api/v1/standards/{sid}", headers=ah)
        assert r.json()["ok"] is True

    async def test_agent_compliance_warning(self, session, monkeypatch):
        """启用规范后，代理产出违反规则时在 warnings 中逐条列出。"""
        ds = DataSource(name=f"规范仓-{uuid.uuid4().hex[:8]}", db_type=DbType.MYSQL,
                        host="127.0.0.1", port=3306, database_name="d", username="r")
        session.add(ds)
        await session.flush()
        t = MetaTable(datasource_id=ds.id, table_name="ods_trade", comment="交易",
                      layer=TableLayer.ODS, partition_cols=["ds"])
        session.add(t)
        await session.flush()
        session.add(MetaColumn(table_id=t.id, column_name="ds", data_type="string"))
        session.add(_std(rules=["layer_prefix_required"]))
        await session.commit()

        final = {"action": "final", "summary": "s", "tables": [{
            "name": "trade_daily", "layer": "dws",   # 缺分层前缀 → 违规
            "ddl": "CREATE TABLE IF NOT EXISTS trade_daily (gmv string) COMMENT '汇总' "
                   "PARTITIONED BY (ds string)",
            "etl_sql": "INSERT OVERWRITE TABLE trade_daily PARTITION(ds='${ds}') "
                       "SELECT gmv FROM ods_trade WHERE ds='${ds}'",
            "source_tables": ["ods_trade"]}]}

        class G:
            async def chat(self, scene, messages, **kw):
                return LLMResponse(text=__import__("json").dumps(final), model="fake")

        monkeypatch.setattr(coding_agent, "gateway", G())
        scope = coding_agent.AgentScope(db=session, datasource_id=ds.id)
        result = await coding_agent.run_agent(coding_agent.MODE_PROPOSE, "建汇总表", scope)
        assert result.payload is not None
        assert any("分层前缀" in w for w in result.warnings)

    async def test_agent_tool_get_dw_standards(self, session):
        session.add(_std())
        await session.commit()
        scope = coding_agent.AgentScope(db=session)
        out = await coding_agent._tool_get_dw_standards(scope, {})
        assert out["standards"] and out["standards"][0]["rules"]
