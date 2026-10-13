"""表关系自动推断（PRD 3.6「自动抽取指标、维度、表关系」）。

真实库里 `insp.get_foreign_keys()` 经常一条都没有（SQLite 演示库、离线数仓表），
只靠外键会导致 TABLE_JOIN 边全空、跨表维度组合被白名单全部拦死。
这里锁住按命名约定补边的行为。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import (DataSource, DbType, DsStatus, EdgeType, KgEdge, MetaColumn, MetaTable,
                           SemanticRole, SourceKind, TableLayer)
from app.kg import builder as kg_builder


async def _table(session, ds, name: str, layer: TableLayer) -> MetaTable:
    t = MetaTable(datasource_id=ds.id, schema_name="main", table_name=name,
                  layer=layer, source=SourceKind.AUTO)
    session.add(t)
    await session.flush()
    return t


async def _col(session, table, name: str, *, pk: bool = False,
               role: SemanticRole = SemanticRole.DIMENSION) -> MetaColumn:
    c = MetaColumn(table_id=table.id, column_name=name, data_type="INTEGER",
                   semantic_role=role, is_primary_key=pk, source=SourceKind.AUTO)
    session.add(c)
    await session.flush()
    return c


@pytest.fixture
async def star(session) -> dict:
    ds = DataSource(name="星型模型", db_type=DbType.SQLITE, database_name=":memory:",
                    status=DsStatus.CONNECTED)
    session.add(ds)
    await session.flush()

    fact = await _table(session, ds, "dwd_order_detail", TableLayer.DWD)
    for name in ("order_id", "region_id", "channel_id", "product_id", "pay_amt"):
        await _col(session, fact, name, pk=(name == "order_id"),
                   role=SemanticRole.METRIC if name == "pay_amt" else SemanticRole.DIMENSION)

    dws = await _table(session, ds, "dws_trade_daily", TableLayer.DWS)
    for name in ("stat_date", "region_id", "channel_id", "gmv"):
        await _col(session, dws, name, role=SemanticRole.TIME if name == "stat_date"
                   else (SemanticRole.METRIC if name == "gmv" else SemanticRole.DIMENSION))

    dim_region = await _table(session, ds, "dim_region", TableLayer.DIM)
    await _col(session, dim_region, "region_id", pk=True)
    await _col(session, dim_region, "region_name")

    dim_channel = await _table(session, ds, "dim_channel", TableLayer.DIM)
    await _col(session, dim_channel, "channel_id", pk=True)
    await _col(session, dim_channel, "channel_name")

    # 无对应维表的孤立表：不应产生任何边
    orphan = await _table(session, ds, "dwd_other", TableLayer.DWD)
    await _col(session, orphan, "region_id")
    await _col(session, orphan, "vendor_id")

    return {"ds": ds, "fact": fact, "dws": dws, "dim_region": dim_region,
            "dim_channel": dim_channel, "orphan": orphan}


async def _join_triples(session) -> set[tuple[str, str, str]]:
    """{(左表名, 右表名, 左键列)}。"""
    names = {str(t.id): t.table_name
             for t in (await session.execute(select(MetaTable))).scalars()}
    out: set[tuple[str, str, str]] = set()
    for e in (await session.execute(select(KgEdge).where(
            KgEdge.edge_type == EdgeType.TABLE_JOIN))).scalars():
        props = e.properties or {}
        out.add((names[str(e.src_id)], names[str(e.dst_id)], props.get("left_key", "")))
    return out


async def test_infer_joins_from_naming_convention(session, star):
    created = await kg_builder.infer_table_joins(session, star["ds"].id)
    triples = await _join_triples(session)
    # 事实表 2 个外键 × 2 张维表 = 4，另加 dwd_other.region_id → dim_region
    assert created == 5, triples
    assert ("dwd_order_detail", "dim_region", "region_id") in triples
    assert ("dwd_order_detail", "dim_channel", "channel_id") in triples
    assert ("dws_trade_daily", "dim_region", "region_id") in triples
    assert ("dws_trade_daily", "dim_channel", "channel_id") in triples


async def test_infer_joins_marks_source_and_type(session, star):
    await kg_builder.infer_table_joins(session, star["ds"].id)
    e = (await session.execute(select(KgEdge).where(
        KgEdge.edge_type == EdgeType.TABLE_JOIN).limit(1))).scalar_one()
    assert e.properties["inferred"] is True
    assert e.properties["join_type"] == "left"
    assert e.properties["join_keys"][1] == e.properties["right_key"]


async def test_infer_joins_only_points_to_dim_layer(session, star):
    """只向维表建边：事实表之间共享 region_id 不代表业务上可以互连。"""
    await kg_builder.infer_table_joins(session, star["ds"].id)
    triples = await _join_triples(session)
    assert all(dst.startswith("dim_") for _, dst, _ in triples), triples
    assert ("dws_trade_daily", "dwd_order_detail", "region_id") not in triples


async def test_infer_joins_skips_columns_without_a_dim_table(session, star):
    """vendor_id 没有对应维表 → 不能凭列名硬造一条指向别处的边。"""
    await kg_builder.infer_table_joins(session, star["ds"].id)
    triples = await _join_triples(session)
    assert not [t for t in triples if t[2] == "vendor_id"], triples


async def test_infer_joins_is_idempotent(session, star):
    """重复扫描不能把边刷成复数份（扫描是幂等的增量任务）。"""
    first = await kg_builder.infer_table_joins(session, star["ds"].id)
    second = await kg_builder.infer_table_joins(session, star["ds"].id)
    assert first == 5 and second == 0
    assert len(await _join_triples(session)) == 5


async def test_infer_joins_ignores_primary_keys(session, star):
    """事实表主键（order_id）不是外键，不能拿去找 dim_order。"""
    await kg_builder.infer_table_joins(session, star["ds"].id)
    triples = await _join_triples(session)
    assert not [t for t in triples if t[2] == "order_id"]
