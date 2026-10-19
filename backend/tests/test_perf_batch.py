"""第二批加固·后端性能回归：N+1 消除与有界并发。

两类断言手段：
1. SQL 语句计数（before_cursor_execute 事件，只数 SELECT）：证明批量化后
   查询数不随实体数线性增长（同一调用加一倍实体，SELECT 数基本不变）；
2. 假客户端的最大同时在飞请求数：证明元数据拉取已并发、且被 Semaphore 限流。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from app.db.models import (AggFunc, AirflowConfig, AzkabanConfig, DataSource, DbType,
                           DimensionType, DsStatus, EdgeType, EntityType, KgDimension,
                           KgEdge, KgMetric, MetaColumn, MetaTable, MetricStatus,
                           ModelingMessage, ModelingSession, Role, SemanticRole,
                           SourceKind, TableLayer)


class SelectCounter:
    """统计 with 窗口内的 SELECT 语句数（挂在 engine 事件上）。"""

    def __init__(self, engine):
        self._engine = engine
        self.n = 0

        def _cb(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                self.n += 1

        self._cb = _cb

    def __enter__(self):
        event.listen(self._engine.sync_engine, "before_cursor_execute", self._cb)
        return self

    def __exit__(self, *exc):
        event.remove(self._engine.sync_engine, "before_cursor_execute", self._cb)
        return False


# ---------- KG 夹具小工具（与 test_kg_service 同一套合成图谱风格） ----------

async def _mk_ds(session) -> DataSource:
    ds = DataSource(name="性能测试源", db_type=DbType.SQLITE, database_name=":memory:",
                    status=DsStatus.CONNECTED)
    session.add(ds)
    await session.flush()
    return ds


async def _mk_table(session, ds, name: str, layer: TableLayer = TableLayer.DWS) -> MetaTable:
    t = MetaTable(datasource_id=ds.id, schema_name="main", table_name=name,
                  layer=layer, source=SourceKind.AUTO)
    session.add(t)
    await session.flush()
    return t


async def _mk_metric(session, table, name: str) -> KgMetric:
    m = KgMetric(name=name, aliases=[f"{name}_alias"], table_id=table.id,
                 agg_function=AggFunc.SUM, unit="元", business_def=f"{name}口径",
                 status=MetricStatus.ACTIVE, source=SourceKind.AUTO)
    session.add(m)
    await session.flush()
    return m


async def _mk_dim(session, name: str, table, column: str = "c1") -> KgDimension:
    d = KgDimension(name=name, aliases=[f"{name}_alias"], dimension_type=DimensionType.CHANNEL,
                    status=MetricStatus.ACTIVE, source=SourceKind.AUTO,
                    related_columns=[{"table_id": str(table.id), "table_name": table.table_name,
                                      "column_name": column}])
    session.add(d)
    await session.flush()
    return d


async def _mk_edge(session, src_type, src_id, dst_type, dst_id, edge_type, props=None):
    session.add(KgEdge(src_type=src_type, src_id=src_id, dst_type=dst_type, dst_id=dst_id,
                       edge_type=edge_type, properties=props or {}, source=SourceKind.AUTO))
    await session.flush()


# ---------- 1. check_constraints：查询数不随指标数增长 ----------

async def test_check_constraints_query_count_constant(session, engine):
    from app.kg import service as kg_service

    ds = await _mk_ds(session)
    table = await _mk_table(session, ds, "dws_perf_cc")
    dim = await _mk_dim(session, "渠道名称", table)

    async def mk_metrics(n: int, prefix: str) -> list[KgMetric]:
        out = []
        for i in range(n):
            m = await _mk_metric(session, table, f"{prefix}指标{i}")
            await _mk_edge(session, EntityType.METRIC, m.id, EntityType.DIMENSION, dim.id,
                           EdgeType.METRIC_DIM_ALLOWED)
            out.append(m)
        return out

    metrics = await mk_metrics(4, "甲")
    with SelectCounter(engine) as c1:
        await kg_service.check_constraints(session, metrics, [dim])

    metrics += await mk_metrics(4, "乙")
    with SelectCounter(engine) as c2:
        v = await kg_service.check_constraints(session, metrics, [dim])

    # 旧实现每指标 3+ 次查询（FORBIDDEN 边 / 白名单边 / JOIN 可达 / 维度全表），
    # 指标翻倍会多 24+ 次 SELECT；批量化后应为常数。
    assert c2.n - c1.n <= 2, f"SELECT 数随指标数线性增长：{c1.n} -> {c2.n}"
    assert [x for x in v if x.severity == "block" and x.rule == "dim_not_allowed"] == []


# ---------- 2. expand_metric_siblings：候选指标逐个全量查询已消除 ----------

async def test_expand_metric_siblings_query_count_constant(session, engine):
    from app.kg import service as kg_service

    ds = await _mk_ds(session)
    dws = await _mk_table(session, ds, "dws_perf_es", TableLayer.DWS)
    ads = await _mk_table(session, ds, "ads_perf_es", TableLayer.ADS)
    dim_t = await _mk_table(session, ds, "dim_perf_es", TableLayer.DIM)
    channel = await _mk_dim(session, "渠道名称", dim_t, "channel_name")
    await _mk_edge(session, EntityType.TABLE, dws.id, EntityType.TABLE, dim_t.id,
                   EdgeType.TABLE_JOIN, {"left_key": "channel_id", "right_key": "channel_id"})

    # ads 上的孤儿指标够不到渠道维 → 触发兄弟扩张；dws 上的兄弟可 JOIN 满足
    orphan = await _mk_metric(session, ads, "退款金额(ads_perf_es)")
    sibling = await _mk_metric(session, dws, "退款金额")

    with SelectCounter(engine) as c1:
        out1 = await kg_service.expand_metric_siblings(session, [orphan], [channel])
    assert {m.name for m in out1} == {"退款金额(ads_perf_es)", "退款金额"}

    # 再加 4 个同词干候选指标：旧实现对每个候选跑 3 次全量查询，新实现走调用级缓存
    for i in range(4):
        await _mk_metric(session, dws, f"退款金额(v{i})")
    with SelectCounter(engine) as c2:
        out2 = await kg_service.expand_metric_siblings(session, [orphan], [channel])

    assert len(out2) == 6
    assert c2.n - c1.n <= 2, f"SELECT 数随候选指标数线性增长：{c1.n} -> {c2.n}"


# ---------- 3. validator：FORBIDDEN×ALLOWED 冲突检测改集合一次性比对 ----------

async def test_validator_conflict_check_query_count_constant(session, engine):
    from app.kg import validator as kg_validator

    ds = await _mk_ds(session)
    table = await _mk_table(session, ds, "dws_perf_val")
    dim = await _mk_dim(session, "地区名称", table)

    async def mk_conflicts(n: int, prefix: str):
        for i in range(n):
            m = await _mk_metric(session, table, f"{prefix}指标{i}")
            await _mk_edge(session, EntityType.METRIC, m.id, EntityType.DIMENSION, dim.id,
                           EdgeType.METRIC_DIM_ALLOWED)
            await _mk_edge(session, EntityType.METRIC, m.id, EntityType.DIMENSION, dim.id,
                           EdgeType.METRIC_DIM_FORBIDDEN)

    await mk_conflicts(3, "丙")
    with SelectCounter(engine) as c1:
        issues1 = await kg_validator.run_validation(session)

    await mk_conflicts(3, "丁")
    with SelectCounter(engine) as c2:
        issues2 = await kg_validator.run_validation(session)

    conflicts2 = [x for x in issues2 if x["category"] == "conflict_dim_rule"]
    assert len(conflicts2) == 6
    # 旧实现每条 FORBIDDEN 边一次冲突查询；批量化后加一倍边不多一次 SELECT
    assert c2.n - c1.n <= 2, f"SELECT 数随 FORBIDDEN 边数线性增长：{c1.n} -> {c2.n}"


# ---------- 4. builder：重建时不再每边一次 SELECT ----------

async def test_builder_rebuild_edges_batched(session, engine):
    from app.connectors.base import ColumnMeta, TableMeta
    from app.kg import builder as kg_builder
    from app.kg.classifier import ColumnClassification

    ds = await _mk_ds(session)
    cols = [ColumnMeta("dt", "TEXT", "日期")]
    classifications: dict = {}
    for i in range(3):
        cols.append(ColumnMeta(f"amt{i}", "DECIMAL", f"金额{i}"))
        classifications[f"amt{i}"] = ColumnClassification(SemanticRole.METRIC, 0.9,
                                                          agg=AggFunc.SUM)
    for i in range(3):
        cols.append(ColumnMeta(f"dim{i}", "TEXT", f"维度{i}"))
        classifications[f"dim{i}"] = ColumnClassification(SemanticRole.DIMENSION, 0.9,
                                                          dimension_type="other")
    tm = TableMeta(schema_name="main", table_name="dws_perf_build", comment="性能测试表",
                   columns=cols)

    meta_table, _ = await kg_builder.upsert_meta(session, ds.id, tm, classifications)
    await kg_builder.build_kg(session, ds.id, meta_table, tm, classifications)

    with SelectCounter(engine) as c:
        await kg_builder.build_kg(session, ds.id, meta_table, tm, classifications)

    # 旧实现重建一次：3 DIM_ON_TABLE + 9 METRIC_DIM_ALLOWED 每边一次 SELECT（共 21+ 次）；
    # 预取差集后只剩：边预取 1 + 列 1 + 指标/维度存在性 6 + 表指标 1 ≈ 9 次
    assert c.n <= 12, f"重建 SELECT 数过多：{c.n}"

    # 幂等性不受批量化影响：两轮构建后仍是 9 条 ALLOWED 边，无重复
    from sqlalchemy import func, select
    n_allowed = (await session.execute(
        select(func.count()).select_from(KgEdge).where(
            KgEdge.edge_type == EdgeType.METRIC_DIM_ALLOWED))).scalar()
    assert n_allowed == 9


# ---------- 5. table_qa.metric_cards：每指标边查询 + 逐表 get 已批量 ----------

async def test_metric_cards_batched(session, engine):
    from app.agent.skills.table_qa import metric_cards

    ds = await _mk_ds(session)
    ta = await _mk_table(session, ds, "dws_perf_card", TableLayer.DWS)
    tb = await _mk_table(session, ds, "ads_perf_card", TableLayer.ADS)
    metrics = []
    for i in range(3):
        m = await _mk_metric(session, ta, f"卡片指标{i}")
        await _mk_edge(session, EntityType.METRIC, m.id, EntityType.TABLE, tb.id,
                       EdgeType.METRIC_ON_TABLE)
        metrics.append(m)

    with SelectCounter(engine) as c:
        cards = await metric_cards(session, metrics)

    # 旧实现 3 次边查询 + 最多 6 次逐表 get；批量化后 2 次 IN 查询
    assert c.n <= 3, f"metric_cards SELECT 数过多：{c.n}"
    assert len(cards) == 3
    for card in cards:
        # 主表 + 边登记表，按汇总层优先（ads 在前）
        assert [t["name"] for t in card["tables"]] == ["ads_perf_card", "dws_perf_card"]


# ---------- 6. modeling_sessions：消息数一条 group_by 聚合 ----------

async def test_list_sessions_message_count_batched(session, engine):
    from app.api.v1 import modeling_sessions as ms_api
    from app.db.models import User

    # 非管理员按 user_id 过滤：全量套件里其它用例可能残留已提交会话，只数自己的
    user = User(username="perf_viewer", password_hash="x", role=Role.VIEWER)
    session.add(user)
    await session.flush()
    uid = user.id
    sessions = []
    for i in range(4):
        s = ModelingSession(user_id=uid, title=f"会话{i}", requirement=f"需求{i}")
        session.add(s)
        await session.flush()
        for j in range(i):
            session.add(ModelingMessage(session_id=s.id, seq=j, role="user",
                                        content=f"消息{j}"))
        sessions.append(s)
    await session.flush()

    viewer = SimpleNamespace(role=Role.VIEWER, id=uid)
    with SelectCounter(engine) as c:
        out = await ms_api.list_sessions(db=session, user=viewer)

    # 旧实现 1 + 4 次 count；聚合后 2 次（会话列表 + group_by）
    assert c.n <= 3, f"list_sessions SELECT 数过多：{c.n}"
    by_title = {r["title"]: r["message_count"] for r in out}
    assert by_title == {"会话0": 0, "会话1": 1, "会话2": 2, "会话3": 3}


# ---------- 7. Azkaban 元数据：有界并发拉取 ----------

class _FakeAzkaban:
    """记录最大同时在飞请求数的假客户端。"""

    def __init__(self):
        self.cur = 0
        self.max_cur = 0

    async def _tick(self):
        self.cur += 1
        self.max_cur = max(self.max_cur, self.cur)
        await asyncio.sleep(0.02)
        self.cur -= 1

    async def list_projects(self):
        return [{"project_id": i + 1, "name": f"proj{i}"} for i in range(4)]

    async def list_flows(self, project):
        await self._tick()
        return [f"flow{i}" for i in range(4)]

    async def fetch_schedule(self, project_id, flow):
        await self._tick()
        return {"cron": "0 0 * * *"}

    async def flow_executions(self, project, flow, limit=10):
        await self._tick()
        return [{"status": "SUCCEEDED", "endTime": 123}]


async def test_azkaban_metadata_fetches_concurrently(session, monkeypatch):
    from app.api.v1 import azkaban as azk_api
    from app.services import azkaban as azk

    session.add(AzkabanConfig(url="http://azk.local", username="u"))
    await session.flush()
    fake = _FakeAzkaban()
    monkeypatch.setattr(azk, "client_from_config", lambda cfg: fake)

    out = await azk_api.metadata(db=session, user=SimpleNamespace())

    # 36 次远程调用串行约 0.72s；并发后同时在飞 >1 且不超过 Semaphore(8)
    assert 1 < fake.max_cur <= 8, f"并发度异常：{fake.max_cur}"
    assert out["configured"] is True
    assert len(out["projects"]) == 4
    for p in out["projects"]:
        assert len(p["flows"]) == 4
        for f in p["flows"]:
            assert f["cron"] == "0 0 * * *"
            assert f["last_status"] == "SUCCEEDED"
            assert f["last_end_time"] == 123


# ---------- 8. Airflow 元数据：有界并发拉取 ----------

class _FakeAirflow:
    def __init__(self):
        self.cur = 0
        self.max_cur = 0

    async def list_dags(self, limit=100):
        return [{"dag_id": f"dag{i}", "is_paused": False,
                 "schedule_interval": {"value": "@daily"}} for i in range(12)]

    async def latest_dag_run(self, dag_id):
        self.cur += 1
        self.max_cur = max(self.max_cur, self.cur)
        await asyncio.sleep(0.02)
        self.cur -= 1
        return {"state": "success", "start_date": "2026-01-01"}


async def test_airflow_metadata_fetches_concurrently(session, monkeypatch):
    from app.api.v1 import airflow as airflow_api
    from app.services import airflow as af

    session.add(AirflowConfig(url="http://af.local", username="u"))
    await session.flush()
    fake = _FakeAirflow()
    monkeypatch.setattr(af, "client_from_config", lambda cfg: fake)

    # 位置参数调用：端点的登录用户依赖形参名为 `_`
    out = await airflow_api.metadata(session, SimpleNamespace())

    assert 1 < fake.max_cur <= 8, f"并发度异常：{fake.max_cur}"
    assert out["configured"] is True and out["total"] == 12
    assert len(out["dags"]) == 12
    for d in out["dags"]:
        assert d["last_run"] == {"state": "success", "start_date": "2026-01-01"}
        assert d["schedule"] == "@daily"


# ---------- 9. LLM 适配器共享 httpx client ----------

@pytest.fixture(autouse=True)
async def _cleanup_shared_llm_clients():
    yield
    from app.llm.adapters import close_shared_clients
    await close_shared_clients()


async def test_shared_client_keyed_by_endpoint():
    from app.llm.adapters import shared_client

    a1 = shared_client("http://a.example/v1")
    a2 = shared_client("http://a.example/v1/")
    b1 = shared_client("http://b.example/v1")
    assert a1 is a2, "同 endpoint 应复用同一 client"
    assert a1 is not b1, "不同 endpoint 不得复用（防跨配置串地址）"


async def test_adapters_reuse_httpx_client_across_calls(monkeypatch):
    import httpx

    from app.llm.adapters.openai_adapter import OpenAIAdapter
    from app.llm.types import ChatMessage, ResolvedConfig
    from tests.llm_stubs import OpenAIHandler, StubServer

    server = StubServer(OpenAIHandler)
    try:
        created = []
        real_cls = httpx.AsyncClient

        def factory(*args, **kwargs):
            created.append(real_cls(*args, **kwargs))
            return created[-1]

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        cfg = ResolvedConfig(config_id="x", protocol="openai", kind="chat",
                             endpoint=server.base_url, api_key="k", model="m",
                             timeout_sec=10, retry_count=0)
        msgs = [ChatMessage(role="user", content="hi")]
        r1 = await OpenAIAdapter().complete(cfg, msgs)
        r2 = await OpenAIAdapter().complete(cfg, msgs)
        assert r1.text == r2.text == "echo:hi"
        assert len(created) == 1, f"两次调用应复用一个 client，实际新建 {len(created)} 个"
    finally:
        server.stop()
