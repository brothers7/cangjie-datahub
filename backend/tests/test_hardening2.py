"""第二批安全加固：Skill 沙箱强化 / 治理与 KG 权限收窄 / 用户管理审计 / 登录时序防枚举。"""
from __future__ import annotations

import io
import time
import uuid
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.security import hash_password
from app.db.models import (AuditLog, DataSource, DatasourcePermission, DbType, DimensionType,
                           EdgeType, EntityType, EtlTask, KgDimension, KgEdge, KgMetric,
                           MetaTable, MetricStatus, Role, SourceKind, TableLayer, User)
from app.services import custom_skills as cs


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


async def _mk_ds(session, tag):
    ds = DataSource(name=f"仓-{tag}-{uuid.uuid4().hex[:8]}", db_type=DbType.MYSQL,
                    host="127.0.0.1", port=3306, database_name="d", username="r")
    session.add(ds)
    await session.flush()
    session._test_created.append(ds)
    return ds


# ================= 1. Skill 沙箱强化 =================

def _zip(files: dict[str, str], compress=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compress) as z:
        for n, c in files.items():
            z.writestr(n, c)
    return buf.getvalue()


_MANIFEST = "name: hack\ntype: python\ntrigger_keywords: ['x']\n"


class TestSkillStaticBlacklist:
    """静态黑名单新增绕过点：http/ftplib/smtplib/ssl/asyncio/pathlib/os.open/io.open。"""

    @pytest.mark.parametrize("src", [
        "import http\n",
        "from http import client\n",
        "import http.client\n",
        "import ftplib\n",
        "import smtplib\n",
        "import ssl\n",
        "import asyncio\n",
        "from asyncio import open_connection\n",
        "import pathlib\n",
        "from pathlib import Path\n",
        "import os\nx = os.open('/etc/passwd', 0)\n",
        "import io\nx = io.open('/etc/passwd')\n",
        "import ctypes\n",
        "import multiprocessing\n",
        "x = eval('1')\n",
        "exec('1')\n",
        "x = __import__('os')\n",
    ])
    def test_bypass_sources_rejected(self, src):
        main = src + "\ndef run(question, ctx):\n    return ''\n"
        with pytest.raises(Exception, match="危险调用"):
            cs.parse_package(_zip({"skill.yaml": _MANIFEST, "main.py": main}))

    def test_legit_skill_still_accepted(self):
        main = ("import json, re\n\n"
                "def run(question, ctx):\n"
                "    return f\"共 {len(ctx['tables'])} 张表\"\n")
        pkg = cs.parse_package(_zip({"skill.yaml": _MANIFEST, "main.py": main}))
        assert pkg.name == "hack"


class TestZipBomb:
    def test_declared_size_checked_before_read(self, monkeypatch):
        """声明大小超限的条目在读取内容之前就被拒绝（防 zip 炸弹）。"""
        class _FakeInfo:
            def __init__(self, size):
                self.file_size = size

        class _FakeZip:
            def __init__(self, buf):
                pass

            def namelist(self):
                return ["skill.yaml", "main.py"]

            def getinfo(self, n):
                return _FakeInfo(100 * 1024 * 1024 if n == "main.py" else 100)

            def read(self, n):
                if n == "main.py":
                    raise AssertionError("超限条目不应被读取")
                return _MANIFEST.encode()

        monkeypatch.setattr(cs.zipfile, "ZipFile", _FakeZip)
        with pytest.raises(Exception, match="zip 炸弹"):
            cs.parse_package(b"fake-zip-bytes")

    def test_highly_compressed_entry_rejected(self):
        """高压缩比条目（声明解压大小超限）整体拒绝。"""
        big = _zip({"skill.yaml": _MANIFEST, "main.py": "A" * 300_000})
        assert len(big) < 64 * 1024   # 压缩后很小，正是 zip 炸弹特征
        with pytest.raises(Exception, match="超过上限"):
            cs.parse_package(big)


class TestSkillPathFence:
    async def test_read_outside_sandbox_denied(self):
        """沙箱内 open('/etc/passwd') 被运行时路径围栏拒绝。"""
        files = {"main.py": "def run(question, ctx):\n"
                            "    with open('/etc/passwd') as f:\n"
                            "        return f.read()[:100]\n"}
        r = await cs.run_python_skill(files, "main.py", "q", {})
        assert not r["ok"], r
        assert "PermissionError" in r["error"] or "沙箱" in r["error"]

    async def test_os_open_outside_sandbox_denied(self):
        files = {"main.py": "import os\n"
                            "def run(question, ctx):\n"
                            "    fd = os.open('/etc/passwd', os.O_RDONLY)\n"
                            "    return os.read(fd, 16)\n"}
        r = await cs.run_python_skill(files, "main.py", "q", {})
        assert not r["ok"], r
        assert "PermissionError" in r["error"] or "沙箱" in r["error"]

    async def test_read_inside_sandbox_allowed(self):
        """沙箱目录内的相对路径读取不受影响（围栏不误伤正常 skill）。"""
        files = {"main.py": "def run(question, ctx):\n"
                            "    with open('helper.txt', encoding='utf-8') as f:\n"
                            "        return f.read().strip()\n",
                 "helper.txt": "沙箱内文件"}
        r = await cs.run_python_skill(files, "main.py", "q", {})
        assert r["ok"], r
        assert "沙箱内文件" in r["result"]

    async def test_stdlib_import_still_works(self):
        """解释器标准库路径只读放行，skill 可正常 import 系统库。"""
        files = {"main.py": "import json, re, collections\n"
                            "def run(question, ctx):\n"
                            "    return json.dumps(sorted(collections.Counter('aab').items()))\n"}
        r = await cs.run_python_skill(files, "main.py", "q", {})
        assert r["ok"], r
        assert '"a"' in r["result"]


# ================= 2. 治理巡检权限收窄 =================

class TestGovernanceNarrowing:
    async def test_viewer_run_scoped_to_accessible_datasources(self, app_client, committed):
        tag = uuid.uuid4().hex[:6]
        admin = await _mk_user(committed, f"gn-adm-{tag}")
        viewer = await _mk_user(committed, f"gn-viw-{tag}", Role.VIEWER)
        owner = await _mk_user(committed, f"gn-own-{tag}")
        ds_a = await _mk_ds(committed, f"a{tag}")   # viewer 无权
        ds_b = await _mk_ds(committed, f"b{tag}")   # viewer 有权
        perm = DatasourcePermission(user_id=viewer.id, datasource_id=ds_b.id, can_query=True)
        committed.add(perm)
        committed._test_created.append(perm)

        tbl_a, tbl_b = f"ads_x_{tag}_a", f"ads_x_{tag}_b"
        task_a, task_b = f"任务A-{tag}", f"任务B-{tag}"
        committed.add(MetaTable(datasource_id=ds_a.id, table_name=tbl_a, layer=TableLayer.ADS))
        committed.add(MetaTable(datasource_id=ds_b.id, table_name=tbl_b, layer=TableLayer.ADS))
        ta = EtlTask(user_id=owner.id, datasource_id=ds_a.id, name=task_a,
                     target_table=tbl_a, layer="ads", etl_sql="SELECT * FROM ods_a")
        tb = EtlTask(user_id=owner.id, datasource_id=ds_b.id, name=task_b,
                     target_table=tbl_b, layer="ads", etl_sql="SELECT * FROM ods_b")
        committed.add(ta)
        committed.add(tb)
        committed._test_created.extend([ta, tb])
        await committed.commit()

        vh = await _login(app_client, viewer.username)
        ah = await _login(app_client, admin.username)

        # viewer 巡检：只见 ds_b 的任务与表，ds_a 的不出现
        r = await app_client.post("/api/v1/governance/run", headers=vh)
        assert r.status_code == 200, r.text
        findings = r.json()["findings"]
        objects = {f["object"] for f in findings}
        assert task_b in objects and tbl_b in objects
        assert task_a not in objects and tbl_a not in objects

        # admin 巡检：两边都能看到
        r = await app_client.post("/api/v1/governance/run", headers=ah)
        assert r.status_code == 200, r.text
        objects = {f["object"] for f in r.json()["findings"]}
        assert task_a in objects and task_b in objects
        assert tbl_a in objects and tbl_b in objects


# ================= 3. KG 接口权限收窄 =================

class TestKgNarrowing:
    async def _seed(self, committed, tag):
        ds_a = await _mk_ds(committed, f"ka{tag}")
        ds_b = await _mk_ds(committed, f"kb{tag}")
        t_a = MetaTable(datasource_id=ds_a.id, table_name=f"dws_{tag}_a", layer=TableLayer.DWS)
        t_b = MetaTable(datasource_id=ds_b.id, table_name=f"dws_{tag}_b", layer=TableLayer.DWS)
        committed.add(t_a)
        committed.add(t_b)
        await committed.flush()
        committed._test_created.extend([t_a, t_b])
        m_a = KgMetric(name=f"m_{tag}_a", table_id=t_a.id, status=MetricStatus.ACTIVE,
                       source=SourceKind.MANUAL)
        m_b = KgMetric(name=f"m_{tag}_b", table_id=t_b.id, status=MetricStatus.ACTIVE,
                       source=SourceKind.MANUAL)
        committed.add(m_a)
        committed.add(m_b)
        dim = KgDimension(name=f"dim_{tag}", dimension_type=DimensionType.OTHER,
                          status=MetricStatus.ACTIVE, source=SourceKind.MANUAL,
                          related_columns=[
                              {"table_id": str(t_a.id), "table_name": t_a.table_name,
                               "column_name": "ca"},
                              {"table_id": str(t_b.id), "table_name": t_b.table_name,
                               "column_name": "cb"},
                          ])
        committed.add(dim)
        await committed.flush()
        committed._test_created.extend([m_a, m_b, dim])
        e_a = KgEdge(src_type=EntityType.METRIC, src_id=m_a.id, dst_type=EntityType.DIMENSION,
                     dst_id=dim.id, edge_type=EdgeType.METRIC_DIM_ALLOWED,
                     properties={}, source=SourceKind.MANUAL)
        e_b = KgEdge(src_type=EntityType.METRIC, src_id=m_b.id, dst_type=EntityType.DIMENSION,
                     dst_id=dim.id, edge_type=EdgeType.METRIC_DIM_ALLOWED,
                     properties={}, source=SourceKind.MANUAL)
        committed.add(e_a)
        committed.add(e_b)
        committed._test_created.extend([e_a, e_b])
        await committed.commit()
        return ds_a, t_a, m_a, m_b, dim

    async def test_dimensions_related_columns_filtered(self, app_client, committed):
        tag = uuid.uuid4().hex[:6]
        admin = await _mk_user(committed, f"kg-adm-{tag}")
        viewer = await _mk_user(committed, f"kg-viw-{tag}", Role.VIEWER)
        ds_a, t_a, m_a, m_b, dim = await self._seed(committed, tag)
        perm = DatasourcePermission(user_id=viewer.id, datasource_id=ds_a.id, can_query=True)
        committed.add(perm)
        committed._test_created.append(perm)
        await committed.commit()

        vh = await _login(app_client, viewer.username)
        ah = await _login(app_client, admin.username)

        # viewer：related_columns 只保留有权数据源（ds_a）的落点
        r = await app_client.get("/api/v1/kg/dimensions", headers=vh)
        d = next(x for x in r.json() if x["name"] == f"dim_{tag}")
        assert [c["table_id"] for c in d["related_columns"]] == [str(t_a.id)]

        # admin：全量保留
        r = await app_client.get("/api/v1/kg/dimensions", headers=ah)
        d = next(x for x in r.json() if x["name"] == f"dim_{tag}")
        assert len(d["related_columns"]) == 2

    async def test_graph_edges_scoped_in_sql(self, app_client, committed):
        tag = uuid.uuid4().hex[:6]
        admin = await _mk_user(committed, f"kg2-adm-{tag}")
        viewer = await _mk_user(committed, f"kg2-viw-{tag}", Role.VIEWER)
        ds_a, t_a, m_a, m_b, dim = await self._seed(committed, tag)
        perm = DatasourcePermission(user_id=viewer.id, datasource_id=ds_a.id, can_query=True)
        committed.add(perm)
        committed._test_created.append(perm)
        await committed.commit()

        vh = await _login(app_client, viewer.username)
        ah = await _login(app_client, admin.username)

        # viewer：无权数据源的指标节点与边不可见
        r = await app_client.get("/api/v1/kg/graph", headers=vh)
        body = r.json()
        node_ids = {n["id"] for n in body["nodes"]}
        assert str(m_a.id) in node_ids and str(dim.id) in node_ids
        assert str(m_b.id) not in node_ids
        edge_pairs = {(e["src"], e["dst"]) for e in body["edges"]}
        assert (str(m_a.id), str(dim.id)) in edge_pairs
        assert (str(m_b.id), str(dim.id)) not in edge_pairs

        # admin：两条边都在
        r = await app_client.get("/api/v1/kg/graph", headers=ah)
        edge_pairs = {(e["src"], e["dst"]) for e in r.json()["edges"]}
        assert (str(m_a.id), str(dim.id)) in edge_pairs
        assert (str(m_b.id), str(dim.id)) in edge_pairs


# ================= 4. 用户管理审计 =================

class TestUserManagementAudit:
    async def test_user_ops_write_audit_log(self, app_client, committed):
        tag = uuid.uuid4().hex[:6]
        admin = await _mk_user(committed, f"au-adm-{tag}")
        ds = await _mk_ds(committed, f"au{tag}")
        await committed.commit()
        ah = await _login(app_client, admin.username)

        # 创建用户
        r = await app_client.post("/api/v1/users", headers=ah,
                                  json={"username": f"au-u-{tag}", "password": "pw123456",
                                        "role": "viewer"})
        assert r.status_code == 200, r.text
        uid = r.json()["id"]

        # 修改角色 / 禁用 / 重置密码
        r = await app_client.patch(f"/api/v1/users/{uid}", headers=ah, json={"role": "analyst"})
        assert r.status_code == 200, r.text
        r = await app_client.patch(f"/api/v1/users/{uid}", headers=ah, json={"status": "disabled"})
        assert r.status_code == 200, r.text
        r = await app_client.patch(f"/api/v1/users/{uid}", headers=ah,
                                   json={"password": "newpw123456"})
        assert r.status_code == 200, r.text

        # 数据源权限分配
        r = await app_client.put(f"/api/v1/users/{uid}/datasource-permissions", headers=ah,
                                 json={"permissions": [{"datasource_id": str(ds.id),
                                                        "can_query": True}]})
        assert r.status_code == 200, r.text

        await committed.commit()   # 结束当前事务，确保读到 API 连接写入的数据
        rows = (await committed.execute(select(AuditLog).where(
            AuditLog.resource_id == uid, AuditLog.resource_type == "user"))).scalars().all()
        by_action: dict[str, list] = {}
        for row in rows:
            by_action.setdefault(row.action, []).append(row)

        assert len(by_action.get("user.create", [])) == 1
        assert by_action["user.create"][0].detail["username"] == f"au-u-{tag}"

        updates = by_action.get("user.update", [])
        assert len(updates) == 3
        changed_sets = [u.detail["changed"] for u in updates]
        assert ["role"] in changed_sets
        assert ["status"] in changed_sets
        assert any("password" in c for c in changed_sets)
        # 审计不落明文密码
        assert all("newpw" not in str(u.detail) for u in updates)

        perms = by_action.get("user.datasource_permissions", [])
        assert len(perms) == 1
        assert perms[0].detail["datasource_ids"] == [str(ds.id)]


# ================= 5. 登录时序防枚举 =================

class TestLoginTiming:
    async def test_unknown_user_runs_dummy_verify(self, app_client, monkeypatch):
        """不存在的账号也执行一次 dummy argon2 校验（且不计入锁定计数）。"""
        from app.api.v1 import auth as auth_mod

        calls: list[str] = []
        orig = auth_mod.verify_password

        def spy(plain, hashed):
            calls.append(hashed)
            return orig(plain, hashed)

        monkeypatch.setattr(auth_mod, "verify_password", spy)
        r = await app_client.post("/api/v1/auth/login",
                                  json={"username": f"ghost-{uuid.uuid4().hex[:8]}",
                                        "password": "wrong"})
        assert r.status_code in (400, 401)
        assert r.json()["message"] == "用户名或密码错误"
        assert calls == [auth_mod._DUMMY_HASH]

    async def test_unknown_user_response_not_fast(self, app_client):
        """未知用户登录耗时不应明显快于一次 argon2 校验（粗粒度时序断言）。"""
        t0 = time.perf_counter()
        r = await app_client.post("/api/v1/auth/login",
                                  json={"username": f"ghost-{uuid.uuid4().hex[:8]}",
                                        "password": "wrong"})
        elapsed = time.perf_counter() - t0
        assert r.status_code in (400, 401)
        assert elapsed > 0.02   # argon2(m=64MB,t=3) 正常需 ~100ms，20ms 为保守下限
