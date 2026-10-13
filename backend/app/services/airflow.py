"""Airflow 适配（PRD 3.3G）：REST API 只读对接（连通性/DAG 列表/最近运行）+ 内置 DAG 文件模板。

Airflow 无官方 DAG 上传 API——不直连写入，任务以「导出 DAG py」方式对接，
人工放入 dags 目录生效。凭证 AES-256-GCM 加密存储，前端永不回显明文。
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.core.crypto import decrypt
from app.db.models import AirflowConfig


class AirflowError(Exception):
    pass


@dataclass
class AirflowClient:
    url: str
    username: str
    password: str
    timeout: float = 15.0
    verify_tls: bool = False           # 校验 TLS 证书（默认关，兼容内网自签名）

    @property
    def base(self) -> str:
        return self.url.rstrip("/") + "/api/v1"

    async def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, verify=self.verify_tls) as cli:
                r = await cli.get(self.base + path, params=params or {},
                                  auth=(self.username, self.password))
        except httpx.HTTPError as e:
            raise AirflowError(f"连接失败：{e.__class__.__name__}") from e
        if r.status_code in (401, 403):
            raise AirflowError("认证失败（请检查账号密码）")
        if r.status_code >= 400:
            raise AirflowError(f"Airflow 返回 {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise AirflowError("响应不是 JSON（地址可能不是 Airflow REST API）") from e

    async def test_connection(self) -> str:
        data = await self._get("/dags", {"limit": 1})
        return f"连接成功（共 {data.get('total_entries', 0)} 个 DAG）"

    async def list_dags(self, limit: int = 100) -> list[dict]:
        data = await self._get("/dags", {"limit": limit, "order_by": "dag_id"})
        return data.get("dags") or []

    async def latest_dag_run(self, dag_id: str) -> dict | None:
        data = await self._get(f"/dags/{dag_id}/dagRuns",
                               {"limit": 1, "order_by": "-start_date"})
        runs = data.get("dag_runs") or []
        return runs[0] if runs else None


def client_from_config(cfg: AirflowConfig) -> AirflowClient:
    return AirflowClient(url=cfg.url, username=cfg.username,
                         password=decrypt(cfg.password_cipher, aad=f"airflow:{cfg.id}") if cfg.password_cipher else "",
                         verify_tls=bool(getattr(cfg, "verify_tls", False)))


# ---------- 内置 DAG 模板（导出对接，不直连写入） ----------

_DAG_TEMPLATE = '''"""仓颉 Cangjie 导出的 Airflow DAG：{name}
目标表：{target_table}（{layer}）  业务日期变量：{{{{ ds }}}}
DDL 不会在数仓自动执行——请先人工审核并执行建表语句（见任务详情）。
"""
from __future__ import annotations

from datetime import datetime

from airflow import DAG
from airflow.operators.bash import BashOperator

with DAG(
    dag_id={dag_id!r},
    description={name!r},
    schedule={schedule},
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["cangjie", {layer!r}],
) as dag:
    run_etl = BashOperator(
        task_id="{task_id}",
        # 按实际引擎替换执行命令；加工 SQL 中的 ${{{{ds}}}} 即 Airflow 的 {{{{ ds }}}}
        bash_command={bash_command!r},
    )
{depends_block}
'''


def build_dag_py(*, name: str, target_table: str, layer: str, cron: str,
                 etl_sql: str, depends_on_ids: list[str] | None = None) -> str:
    """按任务生成 Airflow DAG py 文件内容（内置模板，供人工放入 dags 目录）。"""
    import json
    import re as _re
    # 标识符与文本分别净化/转义，防任务名/表名中的引号与换行破坏生成的 py
    dag_id = _re.sub(r"[^0-9a-zA-Z_]", "_", target_table or "cangjie_task")
    safe_name = json.dumps(str(name)[:200], ensure_ascii=False)[1:-1].replace('"""', "'''")
    schedule = repr(cron) if cron else "None"
    # cron 为空则手工触发；SQL 经 bash -c 透传给引擎 CLI，部署时按实际引擎调整
    bash_command = f"echo '请按实际引擎配置执行命令；加工 SQL 见仓颉任务 {dag_id}'"
    depends_block = ""
    if depends_on_ids:
        ups = "、".join(_re.sub(r"[^0-9a-zA-Z-]", "", str(d)) for d in depends_on_ids)
        depends_block = (f"\n# 上游依赖（任务级，来自仓颉 depends_on 任务 id）：{ups}\n"
                         "# 跨 DAG 依赖请按需添加 ExternalTaskSensor\n")
    return _DAG_TEMPLATE.format(name=safe_name, target_table=dag_id, layer=layer or "ads",
                                dag_id=dag_id, schedule=schedule, task_id=dag_id,
                                bash_command=bash_command, depends_block=depends_block,
                                etl_sql=etl_sql[:2000])
