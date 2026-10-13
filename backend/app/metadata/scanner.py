"""元数据扫描编排：内省 → 差异比对 → 分类 → 元数据落库 → 图谱构建 → 校验报告。

性能（PRD 4.1）：百表内全量 ≤5min；增量靠 meta_hash 跳过未变化表；
分批扫描（scan_batch_size）避免大库压力。
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import delete, select

from app.connectors.base import TableMeta
from app.connectors.registry import get_connector
from app.core.config import settings
from app.db.models import DataSource, MetaColumn, MetaTable, ScanJob, ScanJobStatus, ScanMode
from app.db.models.base import utcnow
from app.kg import builder as kg_builder
from app.kg import classifier as clf
from app.kg.validator import run_validation
from app.metadata.hash import table_hash

logger = logging.getLogger(__name__)


async def scan_datasource(session, ds: DataSource, mode: ScanMode,
                          job: ScanJob | None = None) -> dict:
    """执行扫描。session 由调用方管理事务；job 行实时更新进度。"""
    t0 = time.monotonic()
    connector = get_connector(ds)
    report: dict = {"tables_total": 0, "tables_changed": 0, "entities": {},
                    "errors": [], "duration_sec": 0}

    def _set_progress(pct: int, **kw) -> None:
        if job is not None:
            job.progress = pct
            for k, v in kw.items():
                setattr(job, k, v)

    try:
        table_metas: list[TableMeta] = await connector.introspect()
    except Exception as e:
        report["errors"].append(f"元数据拉取失败: {e}")
        if job is not None:
            job.status = ScanJobStatus.FAILED
            job.error_log = report["errors"]
            job.finished_at = utcnow()
        return report

    # 与既有 meta_tables 比对（增量：跳过 hash 未变化的表）
    existing_tables: dict[str, MetaTable] = {}
    for row in (await session.execute(
            select(MetaTable).where(MetaTable.datasource_id == ds.id))).scalars():
        existing_tables[row.table_name] = row

    report["tables_total"] = len(table_metas)
    _set_progress(5, tables_total=len(table_metas))

    batch = max(5, settings.scan_batch_size)
    processed = 0
    stats = kg_builder.KgBuildStats()

    for i in range(0, len(table_metas), batch):
        for tm in table_metas[i:i + batch]:
            processed += 1
            new_hash = table_hash(tm)
            prev = existing_tables.get(tm.table_name)
            if mode == ScanMode.INCREMENTAL and prev is not None and prev.meta_hash == new_hash:
                prev.last_seen_at = utcnow()
                continue
            report["tables_changed"] += 1
            try:
                # 采样统计（分类质量）：仅对中小表执行，避免大库压力
                stat_cols = [c.column_name for c in tm.columns][:40]
                stats_by_col = await connector.sample_column_stats(tm.table_name, stat_cols)
                classifications = clf.classify_table(tm, stats_by_col)
                meta_table, _created = await kg_builder.upsert_meta(
                    session, ds.id, tm, classifications, stats_by_col)
                meta_table.meta_hash = new_hash
                meta_table.last_seen_at = utcnow()
                await kg_builder.build_kg(session, ds.id, meta_table, tm,
                                          classifications, stats_by_col, stats)
            except Exception as e:  # 单表失败不影响整体
                logger.exception("scan table %s failed", tm.table_name)
                report["errors"].append(f"{tm.table_name}: {e}")
        _set_progress(5 + int(95 * processed / max(1, len(table_metas))))

    # 增量：源库中已消失的表 → 保留元数据与图谱（人工废弃优先），仅记录报告
    gone = set(existing_tables) - {tm.table_name for tm in table_metas}
    if gone and mode == ScanMode.FULL:
        report["errors"].append(f"以下表在源库中已不存在（图谱保留，可人工废弃）: {', '.join(sorted(gone))}")

    # 表关系推断必须在全表扫描结束后做：逐表构建时还不知道后续维表是否存在
    try:
        report["joins_inferred"] = await kg_builder.infer_table_joins(session, ds.id)
    except Exception as e:
        logger.exception("infer table joins failed")
        report["errors"].append(f"表关系推断失败: {e}")

    ds.last_scan_at = utcnow()
    report["entities"] = stats.to_dict()
    report["duration_sec"] = round(time.monotonic() - t0, 2)

    # 图谱质量校验
    try:
        issues = await run_validation(session, ds.id)
        report["validation"] = issues
    except Exception as e:
        report["errors"].append(f"图谱校验失败: {e}")

    if job is not None:
        job.tables_changed = report["tables_changed"]
        job.entities_created = stats.entities_created
        job.entities_updated = stats.entities_updated
        job.entities_skipped_manual = stats.entities_skipped_manual
        job.error_log = report["errors"]
        job.progress = 100
        job.finished_at = utcnow()
        job.status = (ScanJobStatus.SUCCESS if not report["errors"]
                      else ScanJobStatus.PARTIAL)
    return report
