"""PRD 3.3F：AI 数据治理（规则引擎六域 + 巡检快照 + 转任务 + 质量探查权限）。"""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import hash_password
from app.db.models import (DataSource, DbType, EtlTask, MaskingRule, MetaColumn, MetaTable,
                           Role, TableLayer, User)
from app.services import governance as gov


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


async def _mk_table(session, ds, name, layer, comment="", cols=()):
    t = MetaTable(datasource_id=ds.id, table_name=name, comment=comment, layer=layer)
    session.add(t)
    await session.flush()
    for c in cols:
        session.add(MetaColumn(table_id=t.id, column_name=c, data_type="string"))
    return t


class TestRuleEngine:
    async def test_six_domains(self, committed):
        ds = DataSource(name=f"治理仓-{uuid.uuid4().hex[:8]}", db_type=DbType.MYSQL,
                        host="127.0.0.1", port=3306, database_name="d", username="r")
        committed.add(ds)
        await committed.flush()
        # 元数据域：无注释表 + 敏感字段
        await _mk_table(committed, ds, "ods_order", TableLayer.ODS, comment="订单",
                        cols=["order_id", "buyer_phone"])
        # 模型域：无注释 DWS 无任务 + 孤岛
        await _mk_table(committed, ds, "dws_sale_daily", TableLayer.DWS, cols=["gmv"])
        await committed.flush()
        owner = await _mk_user(committed, f"gov-{uuid.uuid4().hex[:6]}")
        # 成本域 + 调度域：SELECT * 无分区 + 无 cron
        committed.add(EtlTask(user_id=owner.id,
            name="订单加工", target_table="dwd_order_di", layer="dwd",
            ddl="CREATE TABLE dwd_order_di (id string)", etl_sql="SELECT * FROM ods_order"))
        # 模型域：两个任务写同一目标
        for i in range(2):
            committed.add(EtlTask(user_id=owner.id, name=f"重复加工{i}",
                                  target_table="ads_dup", layer="ads",
                                  etl_sql="INSERT OVERWRITE TABLE ads_dup PARTITION(ds='${ds}') "
                                          "SELECT a FROM ods_order WHERE ds='${ds}'",
                                  cron="0 0 1 * * ?"))
        # 模型域：反向跨层（dwd 任务引用 ads 来源）
        committed.add(EtlTask(user_id=owner.id, name="反向任务", target_table="dwd_x", layer="dwd",
                              source_tables=[{"table": "ads_dup"}],
                              etl_sql="INSERT OVERWRITE TABLE dwd_x PARTITION(ds='${ds}') "
                                      "SELECT a FROM ads_dup WHERE ds='${ds}'",
                              cron="0 0 1 * * ?"))
        await committed.commit()

        findings = await gov.run_rule_engine(committed, None)
        domains = {f["domain"] for f in findings}
        assert {"metadata", "model", "schedule", "security", "cost"} <= domains
        # 关键治理项
        titles = [f["title"] for f in findings]
        assert "多任务写同目标表（重复加工风险）" in titles
        assert "反向跨层引用" in titles
        assert "疑似敏感字段未配置脱敏" in titles
        assert any(f["title"] == "SELECT * 全列读取" for f in findings)
        assert any(f["title"] == "疑似全表扫描（无分区裁剪）" for f in findings)
        # block 级排最前
        assert findings[0]["level"] == "block"

    async def test_sensitive_masked_not_reported(self, committed):
        ds = DataSource(name=f"治理仓2-{uuid.uuid4().hex[:8]}", db_type=DbType.MYSQL,
                        host="127.0.0.1", port=3306, database_name="d", username="r")
        committed.add(ds)
        await committed.flush()
        await _mk_table(committed, ds, "dwd_user", TableLayer.DWD, comment="用户",
                        cols=["user_id", "phone"])
        committed.add(MaskingRule(datasource_id=ds.id, column_pattern="*phone*"))
        await committed.commit()
        findings = await gov.run_rule_engine(committed, None)
        assert not any(f["title"] == "疑似敏感字段未配置脱敏" and f["object"] == "dwd_user"
                       for f in findings)


class TestGovernanceApi:
    async def test_run_snapshot_and_to_task(self, app_client, committed):
        admin = await _mk_user(committed, f"gadm-{uuid.uuid4().hex[:6]}")
        ds = DataSource(name=f"治理仓3-{uuid.uuid4().hex[:8]}", db_type=DbType.MYSQL,
                        host="127.0.0.1", port=3306, database_name="d", username="r")
        committed.add(ds)
        await committed.flush()
        await _mk_table(committed, ds, "ads_report", TableLayer.ADS)   # 有表无任务
        await committed.commit()
        h = await _login(app_client, admin.username)

        # 巡检（LLM 不可用 → report 为空但规则结论完整）
        r = await app_client.post("/api/v1/governance/run", headers=h)
        assert r.status_code == 200, r.text
        run = r.json()
        assert run["finding_count"] > 0 and run["stats"]
        assert any(f["action"] == "new_task" for f in run["findings"])

        # 历史可回看
        r = await app_client.get("/api/v1/governance/runs", headers=h)
        assert len(r.json()["runs"]) >= 1
        rid = r.json()["runs"][0]["id"]
        r = await app_client.get(f"/api/v1/governance/runs/{rid}", headers=h)
        assert r.json()["findings"]

        # 治理项转任务草稿
        f = next(f for f in run["findings"] if f["action"] == "new_task")
        r = await app_client.post("/api/v1/governance/findings/to-task", headers=h,
                                  json={"title": f["title"], "object": f["object"],
                                        "suggestion": f["suggestion"]})
        assert r.status_code == 200 and r.json()["task_id"]

    async def test_probe_permission_and_quality(self, app_client, committed, tmp_path):
        admin = await _mk_user(committed, f"gadm2-{uuid.uuid4().hex[:6]}")
        analyst = await _mk_user(committed, f"gana-{uuid.uuid4().hex[:6]}", Role.ANALYST)
        # 真实 sqlite 库：主键重复 + 高空值
        db_path = tmp_path / "gov.db"
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE dwd_trade (order_id TEXT, channel TEXT, remark TEXT);
        """)
        rows = [(f"o{i % 50}", f"c{i % 3}", None if i % 10 else "x") for i in range(300)]
        conn.executemany("INSERT INTO dwd_trade VALUES (?,?,?)", rows)
        conn.commit(); conn.close()

        ds = DataSource(name=f"探查仓-{uuid.uuid4().hex[:8]}", db_type=DbType.SQLITE,
                        database_name=str(db_path), username="")
        committed.add(ds)
        await committed.flush()
        t = await _mk_table(committed, ds, "dwd_trade", TableLayer.DWD,
                            cols=["order_id", "channel", "remark"])
        # 主键标注
        from sqlalchemy import update as sa_update
        await committed.execute(sa_update(MetaColumn)
                                .where(MetaColumn.table_id == t.id,
                                       MetaColumn.column_name == "order_id")
                                .values(is_primary_key=True))
        await committed.commit()

        ah = await _login(app_client, admin.username)
        uh = await _login(app_client, analyst.username)

        # 无权限的分析师不可探查
        r = await app_client.post("/api/v1/governance/probe", headers=uh,
                                  json={"datasource_id": str(ds.id), "table": "dwd_trade"})
        assert r.status_code == 403

        # 管理员探查：主键重复 + 空值异常被识别
        r = await app_client.post("/api/v1/governance/probe", headers=ah,
                                  json={"datasource_id": str(ds.id), "table": "dwd_trade"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["stats"]["order_id"]["distinct"] == 50
        issues = "；".join(f["issue"] for f in body["findings"])
        assert "重复" in issues and "空值" in issues
