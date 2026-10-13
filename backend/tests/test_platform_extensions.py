"""PRD 3.3D-4 / 3.3E：任务元数据统一搜索、规范文件导入、调度平台 Skill。"""
from __future__ import annotations

import io
import uuid
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import hash_password
from app.db.models import (CustomSkill, DataSource, DbType, EtlTask, MetaColumn, MetaTable,
                           Role, SkillStatus, SkillType, TableLayer, User)


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


def _skill_zip(name: str, skill_type: str, main_py: str,
               keywords: list[str] | None = None) -> bytes:
    kw = ",".join(f'"{k}"' for k in (keywords or []))
    manifest = (f'name: {name}\ndescription: 测试\ntype: {skill_type}\n'
                f'version: 1\nentry: main.py\ntrigger_keywords: [{kw}]\n')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("skill.yaml", manifest)
        zf.writestr("main.py", main_py)
    return buf.getvalue()


SCHED_MAIN = '''
def build_package(task):
    return {
        "filename": task["target_table"] + "_dag.py",
        "content": "# Airflow DAG for " + task["target_table"] + "\\n# cron: " + (task.get("cron") or "manual"),
        "note": "导入 Airflow dags 目录后启用",
    }
'''


class TestSchedulerSkill:
    async def test_upload_contract(self, app_client, committed):
        """scheduler 型：无需触发词；必须实现 build_package；危险调用仍拦截。"""
        admin = await _mk_user(committed, f"sch-{uuid.uuid4().hex[:6]}")
        h = await _login(app_client, admin.username)

        # 缺契约函数 → 拒绝
        bad = _skill_zip("badsched", "scheduler", "def run(q, c):\n    return 'x'\n")
        r = await app_client.post("/api/v1/skills/upload", headers=h,
                                  files={"file": ("s.zip", bad, "application/zip")})
        assert r.status_code == 422 and "build_package" in r.text

        # 网络调用 → 拒绝（沙箱禁网原则不变）
        net = _skill_zip("netsched", "scheduler",
                         "import requests\ndef build_package(task):\n    return {}\n")
        r = await app_client.post("/api/v1/skills/upload", headers=h,
                                  files={"file": ("s.zip", net, "application/zip")})
        assert r.status_code == 422

        # 合规包 → 成功
        good = _skill_zip("airflow", "scheduler", SCHED_MAIN)
        r = await app_client.post("/api/v1/skills/upload", headers=h,
                                  files={"file": ("s.zip", good, "application/zip")})
        assert r.status_code == 200, r.text
        assert r.json()["skill_type"] == "scheduler"

    async def test_export_via_scheduler_skill(self, app_client, committed):
        """任务经调度 Skill 导出工程文件；平台列表包含内置 Azkaban + Skill。"""
        admin = await _mk_user(committed, f"sch2-{uuid.uuid4().hex[:6]}")
        skill = CustomSkill(name="airflow_exp", description="Airflow 导出", version=1,
                            skill_type=SkillType.SCHEDULER, trigger_keywords=[],
                            files={"skill.yaml": "name: airflow_exp", "main.py": SCHED_MAIN},
                            entry="main.py", status=SkillStatus.ENABLED)
        committed.add(skill)
        task = EtlTask(user_id=admin.id, name="GMV 汇总", target_table="ads_gmv_daily",
                       layer="ads", cron="0 0 2 * * ?",
                       ddl="CREATE TABLE ads_gmv_daily (gmv double)",
                       etl_sql="INSERT OVERWRITE TABLE ads_gmv_daily PARTITION(ds='${ds}') SELECT 1")
        committed.add(task)
        await committed.commit()
        h = await _login(app_client, admin.username)

        # 平台清单：内置 Azkaban + airflow Skill
        r = await app_client.get("/api/v1/etl/tasks/schedulers", headers=h)
        keys = [p["key"] for p in r.json()["platforms"]]
        assert "azkaban" in keys and "skill:airflow_exp" in keys

        # 导出
        r = await app_client.post(f"/api/v1/etl/tasks/{task.id}/export", headers=h,
                                  json={"scheduler": "airflow_exp"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["filename"] == "ads_gmv_daily_dag.py"
        assert "Airflow DAG" in body["content"] and "0 0 2 * * ?" in body["content"]

        # 未启用/不存在的 Skill → 404
        r = await app_client.post(f"/api/v1/etl/tasks/{task.id}/export", headers=h,
                                  json={"scheduler": "nope"})
        assert r.status_code == 404


class TestStandardsImport:
    async def test_import_and_suggest(self, app_client, committed):
        admin = await _mk_user(committed, f"imp-{uuid.uuid4().hex[:6]}")
        h = await _login(app_client, admin.username)
        content = ("# 交易域建模规范\n\n所有表名必须带分层前缀（dws_/ads_）。\n"
                   "建表必须声明分区字段，加工 SQL 必须包含分区过滤 ${ds}。\n"
                   "表与字段必须有中文注释。\n")
        r = await app_client.post("/api/v1/standards/import", headers=h,
                                  files={"file": ("交易域建模规范.md", content.encode(),
                                                  "text/markdown")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "交易域建模规范"
        assert "分层前缀" in body["content"]
        # 启发式建议：前缀 + 分区 + 分区过滤 + 注释
        assert set(body["rules"]) >= {"layer_prefix_required", "partition_required",
                                      "partition_filter_required", "comment_required"}

        # 导入不落库：列表中没有该规范（其他用例可能已建别的规范）
        r = await app_client.get("/api/v1/standards", headers=h)
        assert body["name"] not in [s["name"] for s in r.json()["standards"]]

        # 确认后保存生效
        r = await app_client.post("/api/v1/standards", headers=h,
                                  json={"name": body["name"], "content": body["content"],
                                        "rules": body["rules"], "enabled": True})
        assert r.status_code == 200


class TestTaskMetadata:
    async def test_search_and_permission(self, app_client, committed):
        admin = await _mk_user(committed, f"meta-{uuid.uuid4().hex[:6]}")
        analyst = await _mk_user(committed, f"meta-a-{uuid.uuid4().hex[:6]}", Role.ANALYST)
        ds = DataSource(name=f"元数据仓-{uuid.uuid4().hex[:8]}", db_type=DbType.MYSQL,
                        host="127.0.0.1", port=3306, database_name="d", username="r")
        committed.add(ds)
        await committed.flush()
        mt = MetaTable(datasource_id=ds.id, table_name="dws_trade_daily", comment="交易按天汇总",
                       layer=TableLayer.DWS)
        committed.add(mt)
        await committed.flush()
        committed.add(MetaColumn(table_id=mt.id, column_name="gmv", data_type="double"))
        committed.add(EtlTask(user_id=admin.id, name="交易汇总任务", target_table="dws_trade_daily",
                              layer="dws", cron="0 0 2 * * ?"))
        await committed.commit()
        ah = await _login(app_client, admin.username)
        uh = await _login(app_client, analyst.username)

        # 搜索同时命中任务与表
        r = await app_client.get("/api/v1/modeling/task-metadata?q=交易", headers=ah)
        body = r.json()
        assert any("交易" in t["name"] for t in body["tasks"])
        assert any(t["name"] == "dws_trade_daily" and t["col_count"] == 1 for t in body["tables"])

        # 无权限的分析师看不到表（任务只见到自己的）
        r = await app_client.get("/api/v1/modeling/task-metadata?q=交易", headers=uh)
        body = r.json()
        assert body["tables"] == [] and body["tasks"] == []
