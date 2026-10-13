"""KG 服务回归测试：实体链接 / 表路由 / 口径收窄 / 约束校验。

每个用例都用一个小型合成图谱，不依赖演示库，因此能精确断言打分与边方向。
"""
from __future__ import annotations

import uuid

import pytest

from app.db.models import (AggFunc, DataSource, DbType, DimensionType, DsStatus, EdgeType,
                           EntityType, KgDimension, KgEdge, KgMetric, MetaColumn, MetaTable,
                           MetricStatus, SemanticRole, SourceKind, TableLayer)
from app.kg import service as kg_service


class Fixture:
    """一个数据源 + 三张表（ads/dws/dim）+ 指标/维度/边。"""

    def __init__(self, ds, tables: dict[str, MetaTable], cols: dict[str, MetaColumn],
                 metrics: dict[str, KgMetric], dims: dict[str, KgDimension]):
        self.ds = ds
        self.tables = tables
        self.cols = cols
        self.metrics = metrics
        self.dims = dims

    @property
    def ds_id(self) -> uuid.UUID:
        return self.ds.id


async def _mk_table(session, ds, name: str, layer: TableLayer) -> MetaTable:
    t = MetaTable(datasource_id=ds.id, schema_name="main", table_name=name,
                  layer=layer, source=SourceKind.AUTO)
    session.add(t)
    await session.flush()
    return t


async def _mk_col(session, table, name: str, dtype: str, role: SemanticRole,
                  agg: AggFunc | None = None) -> MetaColumn:
    c = MetaColumn(table_id=table.id, column_name=name, data_type=dtype,
                   semantic_role=role, agg_hint=agg.value if agg else None,
                   source=SourceKind.AUTO)
    session.add(c)
    await session.flush()
    return c


async def _mk_metric(session, table, name: str, aliases: list[str],
                     agg: AggFunc = AggFunc.SUM, unit: str = "元") -> KgMetric:
    m = KgMetric(name=name, aliases=aliases, table_id=table.id, agg_function=agg,
                 unit=unit, business_def=f"{name}口径", status=MetricStatus.ACTIVE,
                 source=SourceKind.AUTO)
    session.add(m)
    await session.flush()
    return m


async def _mk_dim(session, name: str, aliases: list[str], dtype: DimensionType,
                  table, column_name: str) -> KgDimension:
    d = KgDimension(name=name, aliases=aliases, dimension_type=dtype,
                    status=MetricStatus.ACTIVE, source=SourceKind.AUTO,
                    related_columns=[{"table_id": str(table.id), "table_name": table.table_name,
                                      "column_name": column_name}])
    session.add(d)
    await session.flush()
    return d


async def _edge(session, src_type, src_id, dst_type, dst_id, edge_type, props=None):
    session.add(KgEdge(src_type=src_type, src_id=src_id, dst_type=dst_type, dst_id=dst_id,
                       edge_type=edge_type, properties=props or {}, source=SourceKind.AUTO))
    await session.flush()


@pytest.fixture
async def kg(session) -> Fixture:
    ds = DataSource(name="测试源", db_type=DbType.SQLITE, database_name=":memory:",
                    status=DsStatus.CONNECTED)
    session.add(ds)
    await session.flush()

    # DWS 明细表：有时间列 + 渠道维，指标 gmv
    dws = await _mk_table(session, ds, "dws_trade_daily", TableLayer.DWS)
    await _mk_col(session, dws, "stat_date", "TEXT", SemanticRole.TIME)
    await _mk_col(session, dws, "channel_id", "BIGINT", SemanticRole.DIMENSION)
    await _mk_col(session, dws, "gmv", "DECIMAL", SemanticRole.METRIC, AggFunc.SUM)

    # ADS 汇总表：无时间列，只有一行区域排行
    ads = await _mk_table(session, ds, "ads_region_rank", TableLayer.ADS)
    await _mk_col(session, ads, "region_name", "TEXT", SemanticRole.DIMENSION)
    await _mk_col(session, ads, "gmv", "DECIMAL", SemanticRole.METRIC, AggFunc.SUM)

    # 维表
    dim = await _mk_table(session, ds, "dim_channel", TableLayer.DIM)
    await _mk_col(session, dim, "channel_name", "TEXT", SemanticRole.DIMENSION)

    tables = {"dws": dws, "ads": ads, "dim": dim}

    metrics = {
        "gmv_daily": await _mk_metric(session, dws, "日GMV", ["gmv", "成交额"]),
        "gmv_region": await _mk_metric(session, ads, "区域GMV", ["gmv"]),
        "refund": await _mk_metric(session, dws, "退款金额", ["退款"]),
    }
    dims = {
        "channel": await _mk_dim(session, "渠道名称", ["channel_name"], DimensionType.CHANNEL,
                                 dim, "channel_name"),
        "region": await _mk_dim(session, "地区名称", ["region_name"], DimensionType.GEO,
                                ads, "region_name"),
        "channel_id": await _mk_dim(session, "渠道ID", ["channel_id"], DimensionType.CHANNEL,
                                    dws, "channel_id"),
    }
    # DIM_ON_TABLE 边方向：src=维度, dst=表（builder 的约定）
    for d in dims.values():
        for t in tables.values():
            await _edge(session, EntityType.DIMENSION, d.id, EntityType.TABLE, t.id,
                        EdgeType.DIM_ON_TABLE)
    await _edge(session, EntityType.METRIC, metrics["gmv_daily"].id,
                EntityType.TABLE, dws.id, EdgeType.METRIC_ON_TABLE)
    await _edge(session, EntityType.METRIC, metrics["gmv_region"].id,
                EntityType.TABLE, ads.id, EdgeType.METRIC_ON_TABLE)

    # 白名单：日GMV 只允许按渠道名称；区域GMV 只允许按地区名称；退款金额只允许按渠道ID
    await _edge(session, EntityType.METRIC, metrics["gmv_daily"].id,
                EntityType.DIMENSION, dims["channel"].id, EdgeType.METRIC_DIM_ALLOWED)
    await _edge(session, EntityType.METRIC, metrics["gmv_region"].id,
                EntityType.DIMENSION, dims["region"].id, EdgeType.METRIC_DIM_ALLOWED)
    await _edge(session, EntityType.METRIC, metrics["refund"].id,
                EntityType.DIMENSION, dims["channel_id"].id, EdgeType.METRIC_DIM_ALLOWED)
    return Fixture(ds, tables, {}, metrics, dims)


# ---------- 实体链接 ----------

async def test_link_metric_by_exact_name_and_alias(session, kg):
    r = await kg_service.link_entities(session, "最近7天日GMV趋势", kg.ds_id)
    names = {c.name for c in r.metrics}
    assert "日GMV" in names
    assert next(c for c in r.metrics if c.name == "日GMV").confidence >= 0.9


async def test_link_dimension_uses_type_synonym_fallback(session, kg):
    """用户说「各渠道」而不是「渠道名称」——靠类型同义词兜底，否则排行图无维度可用。"""
    r = await kg_service.link_entities(session, "各渠道GMV排行", kg.ds_id)
    assert "渠道名称" in {c.name for c in r.dimensions}


async def test_link_dimension_uses_geo_synonym(session, kg):
    r = await kg_service.link_entities(session, "各区域销售额", kg.ds_id)
    assert "地区名称" in {c.name for c in r.dimensions}


async def test_id_like_dimension_does_not_shadow_business_dimension(session, kg):
    """「渠道ID」若参与表面形式匹配，会因剥掉 ID 后缀而与「渠道名称」撞名。"""
    r = await kg_service.link_entities(session, "各渠道GMV排行", kg.ds_id)
    assert "渠道ID" not in {c.name for c in r.dimensions}


async def test_link_respects_datasource_scope(session, kg):
    """带数据源过滤时，DIM_ON_TABLE 边方向若写反会一条维度都链接不到。"""
    r = await kg_service.link_entities(session, "各渠道GMV排行", kg.ds_id)
    assert r.dimensions, "带数据源过滤时维度为空，说明 DIM_ON_TABLE 边方向取错了"

    other = DataSource(name="另一个源", db_type=DbType.SQLITE, database_name=":memory:",
                       status=DsStatus.CONNECTED)
    session.add(other)
    await session.flush()
    r2 = await kg_service.link_entities(session, "各渠道GMV排行", other.id)
    assert not r2.metrics and not r2.dimensions, "不应链接到其他数据源的实体"


# ---------- 表路由 ----------

async def test_trend_routes_to_table_with_time_column(session, kg):
    """趋势问题必须落到有时间列的表，否则折线图与异常检测全废。

    ADS 汇总表分层优先级更高，若不做 need_time 过滤会盖过 DWS 明细表。
    """
    both = [str(kg.metrics["gmv_daily"].id), str(kg.metrics["gmv_region"].id)]

    # 先确认不筛时间列时，ADS 表确实是有竞争力的候选（否则本用例证明不了什么）
    unfiltered = await kg_service.resolve_tables(session, both, [])
    assert "ads_region_rank" in {c.table_name for c in unfiltered}

    cands = await kg_service.resolve_tables(session, both, [], need_time=True)
    assert cands, "应至少有一个候选表"
    assert cands[0].table_name == "dws_trade_daily"
    assert "ads_region_rank" not in {c.table_name for c in cands}


async def test_without_need_time_ads_layer_still_wins(session, kg):
    """时点类问题不要求时间列，ADS 汇总层应当优先（分层路由）。"""
    cands = await kg_service.resolve_tables(session, [str(kg.metrics["gmv_daily"].id)], [])
    assert cands[0].table_name == "dws_trade_daily"  # 只有一个指标所属的表


async def test_resolve_tables_prefers_dim_coverage(session, kg):
    """两个指标分属两张表时，能直接提供该维度列的表应排在前面。"""
    cands = await kg_service.resolve_tables(
        session, [str(kg.metrics["gmv_daily"].id), str(kg.metrics["gmv_region"].id)],
        [str(kg.dims["region"].id)])
    assert cands[0].table_name == "ads_region_rank"


# ---------- 口径收窄 ----------

async def test_filter_metrics_drops_incompatible_ones(session, kg):
    """一条指标不支持该维度，不该把整条链路拦死 —— 剔除它，保留能跑的。"""
    dims = [kg.dims["channel"]]
    kept, dropped = await kg_service.filter_metrics_for_dims(
        session, [kg.metrics["gmv_daily"], kg.metrics["gmv_region"]], dims)
    assert [m.name for m in kept] == ["日GMV"]
    assert [m.name for m in dropped] == ["区域GMV"]


async def test_filter_metrics_keeps_all_metrics_without_whitelist(session, kg):
    """无白名单边 = 未登记约束，不做收窄也不拦截。"""
    orphan = KgMetric(name="无约束指标", aliases=["zzz"], table_id=kg.tables["dws"].id,
                      status=MetricStatus.ACTIVE, source=SourceKind.AUTO)
    session.add(orphan)
    await session.flush()
    kept, dropped = await kg_service.filter_metrics_for_dims(
        session, [orphan], [kg.dims["channel"]])
    assert kept == [orphan] and dropped == []


async def test_filter_metrics_returns_all_when_none_compatible(session, kg):
    """全部不兼容时原样返回，交给约束校验给出可读报错，而不是静默变成空集。"""
    kept, dropped = await kg_service.filter_metrics_for_dims(
        session, [kg.metrics["gmv_region"]], [kg.dims["channel"]])
    assert kept == [kg.metrics["gmv_region"]] and dropped == []


# ---------- 约束校验 ----------

async def test_check_constraints_blocks_dim_outside_whitelist(session, kg):
    v = await kg_service.check_constraints(
        session, [kg.metrics["gmv_daily"]], [kg.dims["region"]])
    blocked = [x for x in v if x.severity == "block"]
    assert blocked, "白名单外的维度组合应被拦截"
    assert blocked[0].rule == "dim_not_allowed"


async def test_check_constraints_allows_whitelisted_dim(session, kg):
    v = await kg_service.check_constraints(
        session, [kg.metrics["gmv_daily"]], [kg.dims["channel"]])
    assert [x for x in v if x.severity == "block"] == []


async def test_check_constraints_value_range_is_warning_not_block(session, kg):
    """值域只是提醒，不能拦执行 —— 拦截会误伤当月无数据之类的正常场景。"""
    m = kg.metrics["gmv_daily"]
    m.value_range = [0, 1000]
    await session.flush()
    v = await kg_service.check_constraints(session, [m], [])
    warns = [x for x in v if x.rule == "value_range_note"]
    assert warns and warns[0].severity == "warn"
    assert [x for x in v if x.severity == "block"] == []


# ---------- 口径上下文 ----------

async def test_metric_context_includes_definition_and_columns(session, kg):
    ctx = await kg_service.metric_context(
        session, [kg.metrics["gmv_daily"]], kg.tables["dws"], [kg.dims["channel"]])
    assert "日GMV" in ctx and "口径" in ctx
    assert "stat_date" in ctx and "gmv" in ctx
    assert "渠道名称" in ctx


async def test_join_hints_uses_table_join_edges(session, kg):
    await _edge(session, EntityType.TABLE, kg.tables["dws"].id,
                EntityType.TABLE, kg.tables["dim"].id, EdgeType.TABLE_JOIN,
                {"left_key": "channel_id", "right_key": "channel_id"})
    hints = await kg_service.get_join_hints(
        session, [str(kg.tables["dws"].id), str(kg.tables["dim"].id)])
    assert hints and hints[0].left_table == "dws_trade_daily"
    assert hints[0].right_key == "channel_id"
    assert "channel_name" in hints[0].to_prompt()


async def test_join_hints_skips_non_dimension_targets(session, kg):
    """SQL 生成只需要事实表挂维表；指向事实表的边不该进提示词。"""
    await _edge(session, EntityType.TABLE, kg.tables["dws"].id,
                EntityType.TABLE, kg.tables["ads"].id, EdgeType.TABLE_JOIN,
                {"left_key": "gmv", "right_key": "gmv"})
    assert await kg_service.get_join_hints(session, [str(kg.tables["dws"].id)]) == []


# ---------- 跨表维度（星型/雪花模型） ----------

async def _join_dws_to_dim(session, kg):
    await _edge(session, EntityType.TABLE, kg.tables["dws"].id,
                EntityType.TABLE, kg.tables["dim"].id, EdgeType.TABLE_JOIN,
                {"left_key": "channel_id", "right_key": "channel_id",
                 "inferred": True})


async def test_cross_table_dim_is_reachable_via_join(session, kg):
    """「退款金额按渠道名称拆分」：渠道名称在 dim_channel 上，指标在 dws 上。

    没有 TABLE_JOIN 边（演示库不声明外键）时会被白名单拦死，
    有边之后必须放行，否则星型模型一律不可查。
    """
    dims = [kg.dims["channel"]]
    v = await kg_service.check_constraints(session, [kg.metrics["refund"]], dims)
    assert [x for x in v if x.severity == "block"], "无 JOIN 边时应当拦截"

    await _join_dws_to_dim(session, kg)
    v2 = await kg_service.check_constraints(session, [kg.metrics["refund"]], dims)
    assert [x for x in v2 if x.severity == "block"] == [], \
        "可 JOIN 到的维表上的维度不该再被拦截"
    kept, dropped = await kg_service.filter_metrics_for_dims(
        session, [kg.metrics["refund"]], dims)
    assert dropped == []


async def test_cross_table_dim_still_blocked_for_unrelated_table(session, kg):
    """JOIN 只对确实连通的表放行，不能变成万能白名单。"""
    await _join_dws_to_dim(session, kg)
    # 区域GMV 挂在 ads 表上，与 dim_channel 之间没有任何 JOIN 边
    v = await kg_service.check_constraints(session, [kg.metrics["gmv_region"]],
                                           [kg.dims["channel"]])
    assert [x for x in v if x.severity == "block"]


async def test_resolve_tables_counts_joinable_dimension(session, kg):
    """需要 JOIN 的维度也算「该维度可用」，否则表路由会把指标表整个淘汰。"""
    dims = [str(kg.dims["channel"].id)]
    before = await kg_service.resolve_tables(
        session, [str(kg.metrics["refund"].id)], dims)
    assert before == [] or all(c.joined_dims == 0 for c in before)

    await _join_dws_to_dim(session, kg)
    after = await kg_service.resolve_tables(session, [str(kg.metrics["refund"].id)], dims)
    assert after and after[0].table_name == "dws_trade_daily"
    assert after[0].joined_dims >= 1


async def test_metric_context_adds_join_section(session, kg):
    """跨表维度必须把物理列和 JOIN 条件写进提示词，否则 LLM 会按维度中文名分组。"""
    await _join_dws_to_dim(session, kg)
    ctx = await kg_service.metric_context(
        session, [kg.metrics["refund"]], kg.tables["dws"], [kg.dims["channel"]])
    assert "渠道名称" in ctx and "dim_channel.channel_name" in ctx
    assert "channel_id" in ctx and "JOIN" in ctx.upper()


async def test_join_hints_empty_without_edges(session, kg):
    """没有 TABLE_JOIN 边时提示为空（不能凭空造 JOIN 条件）。"""
    assert await kg_service.get_join_hints(session, [str(kg.tables["dws"].id)]) == []


# ---------- 同词干兄弟指标 ----------

async def _mk_sibling(session, table, name: str, aliases: list[str]) -> KgMetric:
    """跨表同名指标：builder 会给后建的加上「(表名)」后缀。"""
    m = KgMetric(name=name, aliases=aliases, agg_function=AggFunc.SUM, unit="元",
                 business_def=f"{name}口径", status=MetricStatus.ACTIVE,
                 table_id=table.id, source=SourceKind.AUTO)
    session.add(m)
    await session.flush()
    await _edge(session, EntityType.METRIC, m.id, EntityType.TABLE, table.id,
                EdgeType.METRIC_ON_TABLE)
    return m


async def test_metric_stem_strips_table_suffix():
    assert kg_service.metric_stem("订单量(trade_daily)") == "订单量"
    assert kg_service.metric_stem("订单量（trade_daily）") == "订单量"
    assert kg_service.metric_stem("订单量") == "订单量"
    assert kg_service.metric_stem("") == ""


async def test_link_matches_metric_by_stem_when_suffixed(session, kg):
    """跨表同名指标会带表后缀，用户只会说词干；不匹配词干这些指标就永远链接不到。"""
    await _mk_sibling(session, kg.tables["dws"], "订单量(ads_region_rank)", ["order_cnt"])
    r = await kg_service.link_entities(session, "本月各区域订单量排行", kg.ds_id)
    hit = next(c for c in r.metrics if c.name == "订单量(ads_region_rank)")
    assert hit.confidence >= 0.8


async def test_expand_siblings_replaces_metric_that_cannot_serve_dim(session, kg):
    """命中的指标挂在提供不了该维度的表上时，补上同词干、JOIN 得通的兄弟指标。

    「退款金额」命中 ads 表（没有渠道列），兄弟指标在 dws 上、可 JOIN dim_channel，
    不补兄弟就会一路走到「拿 ads 表查渠道」的死路。
    """
    orphan = await _mk_sibling(session, kg.tables["ads"], "退款金额(ads_region_rank)", ["退款"])
    dims = [kg.dims["channel"]]
    # dws 需经 JOIN 才能拿到渠道列，ads 与 dim_channel 之间没有任何边
    await _join_dws_to_dim(session, kg)

    before = await kg_service.expand_metric_siblings(session, [orphan], dims)
    stems = {kg_service.metric_stem(m.name) for m in before}
    assert "退款金额" in stems
    assert any(m.table_id == kg.tables["dws"].id for m in before), \
        "应补上 dws 上同词干、可 JOIN 到渠道维的兄弟指标"


async def test_expand_siblings_is_noop_when_metric_already_serves_dim(session, kg):
    """指标本身就能满足维度时不该引入兄弟，否则会凭空多出候选表。"""
    await _join_dws_to_dim(session, kg)
    dims = [kg.dims["channel"]]
    out = await kg_service.expand_metric_siblings(session, [kg.metrics["refund"]], dims)
    assert out == [kg.metrics["refund"]]


async def test_expand_siblings_is_noop_without_dims(session, kg):
    """时点类问题没有维度，不触发扩张。"""
    orphan = await _mk_sibling(session, kg.tables["ads"], "退款金额(ads_region_rank)", ["退款"])
    out = await kg_service.expand_metric_siblings(session, [orphan], [])
    assert out == [orphan]


async def test_filter_drops_unreachable_metric_without_whitelist(session, kg):
    """白名单缺失时，表上既没有维度列、又 JOIN 不到的表仍要剔除 —— 否则必然执行失败。"""
    orphan = await _mk_sibling(session, kg.tables["ads"], "退款金额(ads_region_rank)", ["退款"])
    await _join_dws_to_dim(session, kg)
    kept, dropped = await kg_service.filter_metrics_for_dims(
        session, [orphan, kg.metrics["refund"]], [kg.dims["channel"]])
    assert [m.name for m in kept] == ["退款金额"]
    assert [m.name for m in dropped] == ["退款金额(ads_region_rank)"]


async def test_filter_keeps_unreachable_metrics_when_none_reachable(session, kg):
    """全都不可达时保留原样，交给约束校验给出可读报错，不静默清空。"""
    orphan = await _mk_sibling(session, kg.tables["ads"], "退款金额(ads_region_rank)", ["退款"])
    kept, dropped = await kg_service.filter_metrics_for_dims(
        session, [orphan], [kg.dims["channel"]])
    assert kept == [orphan] and dropped == []


async def test_check_constraints_blocks_metric_whose_table_cannot_reach_dim(session, kg):
    """白名单放行但物理列够不着时给出可读拦截，而不是让数据库抛 ambiguous/unknown column。"""
    orphan = await _mk_sibling(session, kg.tables["ads"], "退款金额(ads_region_rank)", ["退款"])
    await _edge(session, EntityType.METRIC, orphan.id, EntityType.DIMENSION,
                kg.dims["channel"].id, EdgeType.METRIC_DIM_ALLOWED)
    await _join_dws_to_dim(session, kg)
    # 同批次还有一条能跑通的指标 —— 这时拦截才有意义（可以改用另一条）
    v = await kg_service.check_constraints(session, [orphan, kg.metrics["refund"]],
                                           [kg.dims["channel"]])
    rules = {x.rule for x in v if x.severity == "block"}
    assert "dim_column_unreachable" in rules


async def test_check_constraints_does_not_block_lone_unreachable_metric(session, kg):
    """全部指标都够不着维度时放行：图谱缺关系应该暴露在结果里，不是拦到无结果可看。"""
    orphan = await _mk_sibling(session, kg.tables["ads"], "退款金额(ads_region_rank)", ["退款"])
    await _edge(session, EntityType.METRIC, orphan.id, EntityType.DIMENSION,
                kg.dims["channel"].id, EdgeType.METRIC_DIM_ALLOWED)
    v = await kg_service.check_constraints(session, [orphan], [kg.dims["channel"]])
    assert [x for x in v if x.rule == "dim_column_unreachable"] == []


async def test_filter_returns_empty_when_joins_exist_but_nothing_reaches(session, kg):
    """有表关系时「不可达」就是真不可达：返回空集让上层报错，不挑凑数指标。

    图谱登记了表关系说明关系已建全，此时够不着该维度只能说明这条指标
    确实不支持该维度；若仍挑一条跑，SQL 会退化成笛卡尔积，返回看似正常
    实则错误的数据（比报错更糟）。
    """
    await _join_dws_to_dim(session, kg)
    orphan = await _mk_sibling(session, kg.tables["ads"], "退款金额(ads_region_rank)", ["退款"])
    # ads 表与 dim_channel 之间没有任何 TABLE_JOIN 边
    kept, dropped = await kg_service.filter_metrics_for_dims(
        session, [orphan], [kg.dims["channel"]])
    assert kept == [] and dropped == [orphan]
