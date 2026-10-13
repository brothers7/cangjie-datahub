"""图谱质量校验回归测试（PRD 3.2.6）：重复指标 / 注释缺失 / 白名单不可达组合。"""
from __future__ import annotations

import pytest

from app.db.models import (AggFunc, DataSource, DbType, DimensionType, DsStatus, EdgeType,
                           EntityType, KgDimension, KgEdge, KgMetric, KgValidationRule,
                           MetaColumn, MetaTable, MetricStatus, RuleType, SemanticRole,
                           SourceKind, TableLayer)
from app.kg.validator import run_validation


async def _table(session, ds, name, layer):
    t = MetaTable(datasource_id=ds.id, schema_name="main", table_name=name,
                  layer=layer, source=SourceKind.AUTO)
    session.add(t)
    await session.flush()
    return t


async def _col(session, table, name, role, comment=""):
    c = MetaColumn(table_id=table.id, column_name=name, data_type="TEXT",
                   semantic_role=role, comment=comment, source=SourceKind.AUTO)
    session.add(c)
    await session.flush()
    return c


async def _metric(session, table, name, aliases=(), agg=AggFunc.SUM, business_def="人工口径"):
    m = KgMetric(name=name, aliases=list(aliases), table_id=table.id, agg_function=agg,
                 business_def=business_def, status=MetricStatus.ACTIVE, source=SourceKind.AUTO)
    session.add(m)
    await session.flush()
    return m


async def _dim(session, name, table, column_name):
    d = KgDimension(name=name, dimension_type=DimensionType.GEO, status=MetricStatus.ACTIVE,
                    source=SourceKind.AUTO,
                    related_columns=[{"table_id": str(table.id), "table_name": table.table_name,
                                      "column_name": column_name}])
    session.add(d)
    await session.flush()
    return d


async def _edge(session, edge_type, src_type, src_id, dst_type, dst_id, props=None):
    session.add(KgEdge(src_type=src_type, src_id=src_id, dst_type=dst_type, dst_id=dst_id,
                       edge_type=edge_type, properties=props or {}, source=SourceKind.AUTO))
    await session.flush()


@pytest.fixture
async def graph(session):
    ds = DataSource(name="校验源", db_type=DbType.SQLITE, database_name=":memory:",
                    status=DsStatus.CONNECTED)
    session.add(ds)
    await session.flush()
    # dws 明细表（有 region_id）与 ads 汇总表（没有地区列）
    dws = await _table(session, ds, "dws_trade_daily", TableLayer.DWS)
    await _col(session, dws, "region_id", SemanticRole.DIMENSION, "地区ID")
    await _col(session, dws, "gmv", SemanticRole.METRIC, "成交额")
    ads = await _table(session, ds, "ads_region_rank", TableLayer.ADS)
    await _col(session, ads, "order_cnt", SemanticRole.METRIC, "订单量")
    dim = await _table(session, ds, "dim_region", TableLayer.DIM)
    await _col(session, dim, "region_name", SemanticRole.DIMENSION, "地区名称")
    region = await _dim(session, "地区名称", dim, "region_name")
    return {"ds": ds, "dws": dws, "ads": ads, "dim": dim, "region": region}


async def test_validation_reports_unreachable_whitelisted_combo(session, graph):
    """白名单写了地区维度，但指标挂在没有地区列、也 JOIN 不过去的 ads 表上。"""
    m = await _metric(session, graph["ads"], "订单量", ["order_cnt"])
    await _edge(session, EdgeType.METRIC_DIM_ALLOWED, EntityType.METRIC, m.id,
                EntityType.DIMENSION, graph["region"].id)
    issues = await run_validation(session, graph["ds"].id)
    hit = [i for i in issues if i["category"] == "unreachable_dim_combo"]
    assert hit and hit[0]["severity"] == "error"
    assert "地区名称" in hit[0]["message"]


async def test_validation_clears_when_join_edge_added(session, graph):
    """补上表关系后同一条白名单不再报错 —— 报错必须可修，不能是永久噪音。"""
    m = await _metric(session, graph["ads"], "订单量", ["order_cnt"])
    await _edge(session, EdgeType.METRIC_DIM_ALLOWED, EntityType.METRIC, m.id,
                EntityType.DIMENSION, graph["region"].id)
    # 可达性靠 related_columns 的 table_id 判定，ads 表上得有外键列才连得上
    await _col(session, graph["ads"], "region_id", SemanticRole.DIMENSION, "地区ID")
    await _edge(session, EdgeType.TABLE_JOIN, EntityType.TABLE, graph["ads"].id,
                EntityType.TABLE, graph["dim"].id,
                {"left_key": "region_id", "right_key": "region_name"})
    issues = await run_validation(session, graph["ds"].id)
    assert [i for i in issues if i["category"] == "unreachable_dim_combo"] == []


async def test_validation_detects_duplicate_metric(session, graph):
    await _metric(session, graph["ads"], "订单量", ["order_cnt"])
    await _metric(session, graph["dws"], "订单量(dws)", ["order_cnt"])
    issues = await run_validation(session, graph["ds"].id)
    assert [i for i in issues if i["category"] == "duplicate_metric"]


async def test_validation_flags_missing_agg_and_placeholder_def(session, graph):
    await _metric(session, graph["ads"], "无聚合指标", agg=None, business_def="【自动生成】口径待补充")
    issues = {i["category"] for i in await run_validation(session, graph["ds"].id)}
    assert "metric_agg_missing" in issues
    assert "metric_def_placeholder" in issues


async def test_validation_flags_missing_comment_on_unclassified_column(session, graph):
    await _col(session, graph["dws"], "col_x", SemanticRole.UNKNOWN, "")
    issues = await run_validation(session, graph["ds"].id)
    assert [i for i in issues if i["category"] == "missing_comment"]


async def test_validation_detects_allowed_forbidden_conflict(session, graph):
    m = await _metric(session, graph["dws"], "销售额", ["gmv"])
    for et in (EdgeType.METRIC_DIM_ALLOWED, EdgeType.METRIC_DIM_FORBIDDEN):
        await _edge(session, et, EntityType.METRIC, m.id, EntityType.DIMENSION, graph["region"].id)
    issues = await run_validation(session, graph["ds"].id)
    assert [i for i in issues if i["category"] == "conflict_dim_rule"]


async def test_validation_flags_bad_value_range_rule(session, graph):
    session.add(KgValidationRule(rule_type=RuleType.METRIC_VALUE_RANGE, target_type="global",
                                 severity="warn", rule_config={"range": "not-a-range"},
                                 enabled=True, source=SourceKind.MANUAL))
    await session.flush()
    issues = await run_validation(session, graph["ds"].id)
    assert [i for i in issues if i["category"] == "invalid_rule_config"]
