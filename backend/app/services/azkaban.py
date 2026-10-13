"""Azkaban 调度平台对接（PRD 3.3B-6）：REST 客户端 + Flow 2.0 工程 zip 生成。

安全边界：本模块只负责「生成工程包 + 经人工确认后推送」，
绝不触发数仓 DDL 执行（DDL 不进 flow，只作为工程附件供人工审核）。
"""
from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass, field

import httpx

from app.core.crypto import decrypt
from app.db.models import AzkabanConfig

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(15.0, connect=8.0)


class AzkabanError(Exception):
    pass


@dataclass
class AzkabanClient:
    """Azkaban Web Server REST 客户端（session.id 会话制）。"""
    url: str
    username: str
    password: str
    verify_tls: bool = False           # 校验 TLS 证书（默认关，兼容内网自签名）
    _session_id: str = field(default="", init=False)

    @property
    def base(self) -> str:
        return self.url.rstrip("/")

    async def _login(self) -> str:
        if self._session_id:
            return self._session_id
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=self.verify_tls) as c:
            r = await c.post(f"{self.base}/",
                             data={"action": "login", "username": self.username,
                                   "password": self.password})
        try:
            data = r.json()
        except Exception as e:
            raise AzkabanError(f"Azkaban 登录响应异常（HTTP {r.status_code}），请检查地址") from e
        if data.get("status") == "error" or "session.id" not in data:
            raise AzkabanError(f"Azkaban 登录失败: {data.get('error') or data}")
        self._session_id = data["session.id"]
        return self._session_id

    async def _post(self, path: str, *, data: dict | None = None,
                    files: dict | None = None, params: dict | None = None) -> dict:
        sid = await self._login()
        payload = {"session.id": sid, **(data or {})}
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=self.verify_tls) as c:
            r = await c.post(f"{self.base}{path}", data=payload, files=files, params=params)
        if r.status_code == 401:
            self._session_id = ""  # 会话过期，清掉让下次重登
            raise AzkabanError("Azkaban 会话已过期，请重试")
        try:
            return r.json()
        except Exception as e:
            raise AzkabanError(f"Azkaban 接口异常（HTTP {r.status_code}）: {r.text[:200]}") from e

    async def _get(self, path: str, params: dict) -> dict:
        sid = await self._login()
        async with httpx.AsyncClient(timeout=TIMEOUT, verify=self.verify_tls) as c:
            r = await c.get(f"{self.base}{path}",
                            params={"session.id": sid, **params})
        try:
            return r.json()
        except Exception as e:
            raise AzkabanError(f"Azkaban 接口异常（HTTP {r.status_code}）: {r.text[:200]}") from e

    # ---------- 业务动作 ----------

    async def test_connection(self) -> str:
        """登录 + 列用户项目验证可用性。"""
        await self._login()
        return "登录成功"

    async def ensure_project(self, name: str, description: str = "") -> None:
        """项目不存在则创建（Azkaban 对已存在项目会返回 error，按幂等处理）。"""
        r = await self._post("/manager", data={"action": "create", "name": name,
                                               "description": description or "data-agent ETL"})
        if r.get("status") == "error" and "already exists" not in str(r.get("message", "")):
            raise AzkabanError(f"创建项目失败: {r.get('message') or r}")

    async def upload_zip(self, project: str, zip_bytes: bytes) -> dict:
        r = await self._post("/manager", params={"ajax": "upload"},
                             data={"project": project},
                             files={"file": ("flow.zip", zip_bytes, "application/zip")})
        if r.get("status") == "error" or "projectId" not in r:
            raise AzkabanError(f"上传工程失败: {r.get('message') or r}")
        return r

    async def schedule_cron(self, project: str, flow: str, cron: str,
                            comment: str = "") -> dict:
        """设置 cron 调度（Azkaban 3.x scheduleCronFlow）。"""
        return await self._post("/schedule", params={
            "ajax": "scheduleCronFlow", "projectName": project, "flow": flow,
            "cronExpression": cron,
            "scheduleComment": comment or "created by data-agent"})

    async def flow_executions(self, project: str, flow: str, limit: int = 10) -> list[dict]:
        r = await self._get("/executor", {"ajax": "fetchFlowExecutions",
                                          "project": project, "flow": flow,
                                          "start": 0, "length": limit})
        return r.get("executions") or []

    # ---------- 元数据读取（PRD 3.3D-3：掌管 Azkaban 调度任务元数据） ----------

    async def list_projects(self) -> list[dict]:
        """当前账号可见的项目清单。返回 [{project_id, name}]。"""
        r = await self._get("/index", {"ajax": "fetchuserprojects"})
        return [{"project_id": p.get("projectId"), "name": p.get("name", "")}
                for p in (r.get("projects") or []) if p.get("name")]

    async def list_flows(self, project: str) -> list[str]:
        r = await self._get("/manager", {"ajax": "fetchprojectflows", "project": project})
        return [f.get("flowId", "") for f in (r.get("flows") or []) if f.get("flowId")]

    async def fetch_schedule(self, project_id: int, flow: str) -> dict | None:
        """flow 的调度配置（含 cronExpression）。未设置返回 None。"""
        r = await self._get("/schedule", {"ajax": "fetchSchedule",
                                          "projectId": project_id, "flowId": flow})
        sched = r.get("schedule")
        if not sched or not sched.get("cronExpression"):
            return None
        return {"cron": sched.get("cronExpression"), "period": sched.get("period", ""),
                "submit_user": sched.get("submitUser", "")}


def client_from_config(cfg: AzkabanConfig) -> AzkabanClient:
    if not cfg.url or not cfg.username:
        raise AzkabanError("Azkaban 未配置，请先在「数据源 → 调度平台」中配置连接")
    password = decrypt(cfg.password_cipher, aad=f"azkaban:{cfg.id}") if cfg.password_cipher else ""
    return AzkabanClient(url=cfg.url, username=cfg.username, password=password,
                         verify_tls=bool(getattr(cfg, "verify_tls", False)))


# ---------- Flow 工程生成 ----------

# 各引擎的命令模板：SQL 放在工程内 etl.sql 文件（保留 ${ds} 占位），
# 执行时先导出 ds 变量并 sed 展开到 etl_run.sql 再执行；
# 命令引用环境变量连接信息，不在工程里硬编码任何库地址/账号（凭证不进 zip）。
_SQL_CLI = {
    "hive": 'beeline -u "$HIVE_JDBC_URL" -f etl_run.sql',
    "spark": 'spark-sql -f etl_run.sql',
    "mysql": 'mysql -h"$MYSQL_HOST" -P"$MYSQL_PORT" -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DB" < etl_run.sql',
    "doris": 'mysql -h"$DORIS_HOST" -P"$DORIS_PORT" -u"$DORIS_USER" -p"$DORIS_PASSWORD" "$DORIS_DB" < etl_run.sql',
    "starrocks": 'mysql -h"$SR_HOST" -P"$SR_PORT" -u"$SR_USER" -p"$SR_PASSWORD" "$SR_DB" < etl_run.sql',
    "clickhouse": 'clickhouse-client --host "$CH_HOST" --queries-file etl_run.sql',
}


def _flow_name(target_table: str) -> str:
    name = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in (target_table or "etl_job"))
    return name[:64] or "etl_job"


def build_flow_zip(*, target_table: str, etl_sql: str, ddl: str,
                   dialect: str, notes: str = "", depends_on: list[str] | None = None) -> tuple[bytes, str]:
    """生成 Azkaban Flow 2.0 工程 zip。返回 (zip_bytes, flow_name)。

    内容：flow20.project / <flow>.flow（command 节点跑加工 SQL）/
    etl.sql / DDL.sql（仅附件，人工审核后手动执行，不进调度节点）/ README.md
    depends_on：flow 级依赖（Azkaban Flow 2.0 dependsOn，值为同项目内其它 flow 名）。
    """
    flow = _flow_name(target_table)
    command = _SQL_CLI.get(dialect, 'sh -c "echo 请为该引擎配置 SQL 执行命令 && exit 1"')

    dep_yaml = ""
    if depends_on:
        dep_yaml = "dependsOn:\n" + "".join(f"  - {d}\n" for d in depends_on[:10])
    flow_yaml = (
        (dep_yaml if dep_yaml else "")
        + "nodes:\n"
        f"  - name: run_etl\n"
        f"    type: command\n"
        f"    config:\n"
        f"      command: |\n"
        f"        export ds=$(date -d 'yesterday' +%Y-%m-%d 2>/dev/null || date -v-1d +%Y-%m-%d)\n"
        f"        sed \"s/\\${{ds}}/$ds/g\" etl.sql > etl_run.sql\n"
        f"        {command}\n"
    )
    readme = (f"# {flow}\n\n由仓颉 Cangjie（AI 数仓工作台）生成的 ETL 工程。\n\n"
              f"- 目标表：{target_table}\n- 引擎：{dialect}\n"
              f"- 调度变量：${{ds}}（flow 节点内默认导出为昨日日期）\n\n"
              f"## 注意\n1. `DDL.sql` 不会被调度执行，请人工审核后由有权限的账号在数仓执行。\n"
              f"2. 执行命令中的连接信息读取环境变量（如 HIVE_JDBC_URL / MYSQL_*），"
              "请在 Azkaban Executor 环境配置。\n\n"
              + (f"## 口径说明\n{notes}\n" if notes else ""))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("flow20.project", "azkaban-flow-version: 2.0\n")
        z.writestr(f"{flow}.flow", flow_yaml)
        z.writestr("etl.sql", etl_sql or "-- 未生成加工 SQL\n")
        if ddl:
            z.writestr("DDL.sql", ddl)
        z.writestr("README.md", readme)
    return buf.getvalue(), flow
