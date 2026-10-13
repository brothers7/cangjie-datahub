"""扫描器端到端：SQLite 数据源全量扫描 → 元数据入库 → KG 构建 → 增量重扫不覆盖人工口径。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import (DataSource, DbType, KgDimension, KgMetric, MetaTable, MetricStatus,
                           ScanMode)
from app.metadata.scanner import scan_datasource


@pytest.fixture
def demo_sqlite(tmp_path) -> Path:
    """建一个小型 SQLite 数仓：维度表 + 事实表。"""
    path = tmp_path / "wh.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE dim_region (region_id INTEGER PRIMARY KEY, region_name TEXT);
        CREATE TABLE dwd_orders (
            order_id INTEGER PRIMARY KEY, dt TEXT, region_id INTEGER,
            gmv REAL, order_cnt INTEGER, buyer_phone TEXT
        );
        INSERT INTO dim_region VALUES (1,'华东'),(2,'华北');
        INSERT INTO dwd_orders (dt, region_id, gmv, order_cnt, buyer_phone) VALUES
            ('2026-10-11', 1, 100.5, 2, '13800001111'),
            ('2026-10-11', 2, 200.0, 3, '13900002222');
    """)
    conn.commit()
    conn.close()
    return path


async def _mk_ds(session, path):
    import uuid
    ds = DataSource(name=f"扫描测试-{uuid.uuid4().hex[:8]}", db_type=DbType.SQLITE,
                    database_name=str(path), password_cipher="", username="")
    session.add(ds)
    await session.flush()
    return ds


class TestScanPipeline:
    async def test_full_scan_builds_metadata_and_kg(self, session, demo_sqlite):
        ds = await _mk_ds(session, demo_sqlite)
        report = await scan_datasource(session, ds, ScanMode.FULL)
        await session.commit()

        assert report["errors"] == []
        assert report["tables_total"] == 2

        tables = (await session.execute(
            select(MetaTable).where(MetaTable.datasource_id == ds.id))).scalars().all()
        assert {t.table_name for t in tables} == {"dim_region", "dwd_orders"}

        # 自动识别指标（gmv/order_cnt 是可聚合数值字段）
        metrics = (await session.execute(
            select(KgMetric).join(MetaTable, KgMetric.table_id == MetaTable.id)
            .where(MetaTable.datasource_id == ds.id))).scalars().all()
        assert len(metrics) >= 2

        # 自动识别维度（region_id 关联 dim_region）
        dims = (await session.execute(select(KgDimension))).scalars().all()
        assert len(dims) >= 1

    async def test_incremental_rescan_keeps_manual_edits(self, session, demo_sqlite):
        ds = await _mk_ds(session, demo_sqlite)
        await scan_datasource(session, ds, ScanMode.FULL)
        await session.commit()

        # 人工修正某个指标口径
        m = (await session.execute(
            select(KgMetric).join(MetaTable, KgMetric.table_id == MetaTable.id)
            .where(MetaTable.datasource_id == ds.id).limit(1))).scalars().first()
        assert m is not None
        from app.kg.merger import mark_edited
        m.business_def = "人工口径：含税GMV"
        mark_edited(m, ["business_def"])
        await session.commit()

        # 增量重扫
        report = await scan_datasource(session, ds, ScanMode.INCREMENTAL)
        await session.commit()
        assert report["errors"] == []

        # 人工口径未被覆盖
        m2 = await session.get(KgMetric, m.id)
        assert m2.business_def == "人工口径：含税GMV"

    async def test_scan_missing_db_reports_error(self, session, tmp_path):
        ds = await _mk_ds(session, tmp_path / "nonexistent.db")
        ds.database_name = str(tmp_path)  # 传目录而非文件，introspect 会失败
        report = await scan_datasource(session, ds, ScanMode.FULL)
        assert report["errors"]  # 有错误记录
