"""PRD 3.3D 自动化建模：ETL 子 Agent 校验 / 任务版本快照与回滚 / 权限。"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.agent.skills import etl
from app.core.security import hash_password
from app.db.models import DataSource, DbType, MetaColumn, MetaTable, Role, TableLayer, User


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


class TestSqlglotCheck:
    def test_syntax_ok(self):
        errs = etl.sqlglot_check(
            "CREATE TABLE dws_a (dt string, gmv double)",
            "INSERT OVERWRITE TABLE dws_a PARTITION(dt='${ds}') SELECT dt, SUM(gmv) FROM dwd_b GROUP BY dt",
            "hive", {"dwd_b": {"dt", "gmv"}})
        assert errs == []

    def test_syntax_error(self):
        # sqlglot 对乱码宽容（解析为 Alias），靠语句类型白名单拦截
        errs = etl.sqlglot_check("", "SELEC 错误语法", "hive", {})
        assert any("无法识别为有效 SQL" in e for e in errs)
        errs2 = etl.sqlglot_check("", "INSERT OVERWRITE TABLE dws_a SELECT dt FROM", "hive", {})
        assert any("语法解析失败" in e for e in errs2)

    def test_unknown_table_and_column(self):
        errs = etl.sqlglot_check(
            "",
            "INSERT OVERWRITE TABLE dws_a PARTITION(dt='${ds}') "
            "SELECT a.dt, a.not_exist FROM dwd_ghost a GROUP BY a.dt",
            "hive", {"dwd_b": {"dt", "gmv"}})
        assert any("不存在的表" in e and "dwd_ghost" in e for e in errs)
        assert any("not_exist" in e for e in errs)

    def test_target_table_not_flagged(self):
        # 目标表自身（INSERT 写入的表）不参与「源表存在性」校验
        errs = etl.sqlglot_check(
            "CREATE TABLE dws_new (x int)",
            "INSERT OVERWRITE TABLE dws_new SELECT x FROM dwd_src",
            "mysql", {"dwd_src": {"x"}})
        assert errs == []


class FakeCtx:
    """捕获子 Agent 步骤的最小上下文（duck-typing PipelineContext.step/emit_step）。"""
    def __init__(self):
        self.steps = []

    def step(self, name, detail=""):
        from app.agent.pipeline.context import StepTrace
        st = StepTrace(name, detail=detail)
        self.steps.append(st)
        return st

    def emit_step(self, st, detail=""):
        pass


class TestEtlAgentDegrade:
    async def test_steps_emitted_without_llm(self, session):
        """LLM 不可用时子 Agent 降级，但 etl_plan/etl_draft/etl_check 步骤齐全。"""
        import uuid
        ds = DataSource(name=f"建模-{uuid.uuid4().hex[:8]}", db_type=DbType.MYSQL,
                        host="127.0.0.1", port=3306, database_name="d", username="r")
        session.add(ds)
        await session.flush()
        t = MetaTable(datasource_id=ds.id, table_name="dwd_trade", comment="交易明细",
                      layer=TableLayer.DWD, partition_cols=["dt"])
        session.add(t)
        await session.flush()
        session.add(MetaColumn(table_id=t.id, column_name="dt", data_type="string"))
        await session.commit()

        ctx = FakeCtx()
        design = await etl.etl_design(session=session, question="新建一张按天汇总GMV的表",
                                      datasource=ds, ctx=ctx)
        names = [s.step for s in ctx.steps]
        assert "etl_plan" in names and "etl_draft" in names and "etl_check" in names
        assert design.warnings  # 有 LLM 失败告警
        assert design.to_markdown()


class TestEtlTaskApi:
    async def test_crud_version_rollback_validate(self, app_client, committed):
        await _mk_user(committed, "modeler1")
        h = await _login(app_client, "modeler1")

        # 创建（初始版本 v1 快照）
        r = await app_client.post("/api/v1/etl/tasks", json={
            "name": "日汇总", "target_table": "dws_trade_daily", "dialect": "hive",
            "ddl": "CREATE TABLE dws_trade_daily (dt string, gmv double)",
            "etl_sql": "INSERT OVERWRITE TABLE dws_trade_daily PARTITION(dt='${ds}') SELECT dt, SUM(gmv) FROM dwd_trade GROUP BY dt",
            "source": "manual"}, headers=h)
        assert r.status_code == 200, r.text
        tid = r.json()["id"]
        assert r.json()["status"] == "draft"

        # 编辑 SQL → 旧内容留档 v2
        r = await app_client.patch(f"/api/v1/etl/tasks/{tid}",
                                   json={"etl_sql": "INSERT OVERWRITE TABLE dws_trade_daily SELECT 1"},
                                   headers=h)
        assert r.status_code == 200
        vers = (await app_client.get(f"/api/v1/etl/tasks/{tid}/versions", headers=h)).json()
        assert len(vers) == 2  # 创建时 v1 + 编辑前留档 v2

        # 回滚到 v1（GROUP BY 版本）
        r = await app_client.post(f"/api/v1/etl/tasks/{tid}/rollback",
                                  json={"version": 1}, headers=h)
        assert r.status_code == 200
        assert "GROUP BY" in r.json()["etl_sql"]

        # 校验：合法 SQL 应无语法错误
        r = await app_client.post(f"/api/v1/etl/tasks/{tid}/validate", headers=h)
        assert r.status_code == 200
        assert r.json()["ok"] is True

        # 清空 SQL 后校验报错
        await app_client.patch(f"/api/v1/etl/tasks/{tid}",
                               json={"etl_sql": "SELEC 错误语法"}, headers=h)
        r = await app_client.post(f"/api/v1/etl/tasks/{tid}/validate", headers=h)
        assert r.json()["ok"] is False
        assert any("语法" in w for w in r.json()["warnings"])

        # 清理
        r = await app_client.delete(f"/api/v1/etl/tasks/{tid}", headers=h)
        assert r.status_code == 200

    async def test_permission_isolation(self, app_client, committed):
        await _mk_user(committed, "modeler_a")
        await _mk_user(committed, "modeler_b")
        ha = await _login(app_client, "modeler_a")
        hb = await _login(app_client, "modeler_b")
        r = await app_client.post("/api/v1/etl/tasks", json={
            "name": "A的任务", "etl_sql": "SELECT 1"}, headers=ha)
        tid = r.json()["id"]
        # B 看不到也改不了 A 的任务
        assert all(t["id"] != tid for t in (await app_client.get("/api/v1/etl/tasks", headers=hb)).json())
        r = await app_client.patch(f"/api/v1/etl/tasks/{tid}", json={"name": "劫持"}, headers=hb)
        assert r.status_code == 403
        await app_client.delete(f"/api/v1/etl/tasks/{tid}", headers=ha)

    async def test_push_without_azkaban_degrades(self, app_client, committed):
        await _mk_user(committed, "modeler_c")
        h = await _login(app_client, "modeler_c")
        r = await app_client.post("/api/v1/etl/tasks", json={
            "name": "推送测试", "etl_sql": "INSERT OVERWRITE TABLE t SELECT 1"}, headers=h)
        tid = r.json()["id"]
        r = await app_client.post(f"/api/v1/etl/tasks/{tid}/push", json={}, headers=h)
        assert r.status_code == 200
        assert r.json()["ok"] is False and "未配置 Azkaban" in r.json()["message"]
        await app_client.delete(f"/api/v1/etl/tasks/{tid}", headers=h)


class TestFlowZipDependsOn:
    def test_depends_on_in_yaml(self):
        import io
        import zipfile
        from app.services import azkaban as azk
        zip_bytes, flow = azk.build_flow_zip(
            target_table="dws_a", etl_sql="INSERT OVERWRITE TABLE dws_a SELECT 1",
            ddl="", dialect="mysql", depends_on=["dwd_trade_sync", "dim_channel_sync"])
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        y = zf.read(f"{flow}.flow").decode()
        assert "dependsOn:" in y and "dwd_trade_sync" in y
