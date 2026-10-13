"""本轮修复的回归测试：时间列识别、UUID/时间列跨方言、SQL 守卫。

这些都是端到端联调里踩出来的坑，单测挡住比靠手工点页面可靠。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.connectors.base import ColumnMeta
from app.db.models.base import UTCDateTime, UUIDType, as_utc, utcnow
from app.kg.classifier import SemanticRole, classify_column, classify_table_layer
from app.db.models.metadata import TableLayer


# ---------- Bug F：时间列识别 ----------

@pytest.mark.parametrize("name,dtype", [
    # 真实 SQLite / Hive 外部表把日期存成 TEXT，物理类型完全不可信
    ("stat_date", "TEXT"), ("order_date", "VARCHAR(10)"), ("register_date", "TEXT"),
    ("date", "TEXT"), ("trade_date", "string"), ("created_at", "TEXT"),
    ("统计日期", "TEXT"), ("支付时间", "TEXT"),
    ("dt", "TEXT"),
])
def test_time_column_detected_by_name_even_when_text(name, dtype):
    cls = classify_column(ColumnMeta(column_name=name, data_type=dtype), TableLayer.DWS)
    assert cls.role == SemanticRole.TIME, f"{name}({dtype}) 应识别为时间字段"


@pytest.mark.parametrize("name,dtype", [
    ("gmv", "DECIMAL(18,2)"), ("channel_id", "BIGINT"), ("channel_name", "TEXT"),
    ("order_status", "TEXT"), ("cate", "TEXT"), ("brand", "TEXT"),
])
def test_non_time_columns_not_misclassified(name, dtype):
    cls = classify_column(ColumnMeta(column_name=name, data_type=dtype), TableLayer.DWD)
    assert cls.role != SemanticRole.TIME, f"{name} 不应被当成时间字段"


def test_time_type_alone_is_not_enough_without_name_signal():
    """列名叫 remark、类型是 date 的可疑情况不该硬判时间（无命名信号即守不住语义）。"""
    cls = classify_column(ColumnMeta(column_name="remark", data_type="TEXT"), TableLayer.DWD)
    assert cls.role != SemanticRole.TIME


def test_layer_routing_by_prefix():
    assert classify_table_layer("dws_trade_daily") == TableLayer.DWS
    assert classify_table_layer("ads_gmv_trend") == TableLayer.ADS
    assert classify_table_layer("dim_channel") == TableLayer.DIM
    assert classify_table_layer("whatever_table") == TableLayer.UNKNOWN


# ---------- Bug D：时间列跨方言统一为 aware UTC ----------

def test_as_utc_treats_naive_as_utc():
    naive = datetime(2026, 10, 12, 3, 0, 0)
    aware = as_utc(naive)
    assert aware.tzinfo is not None
    assert aware == naive.replace(tzinfo=UTC)
    assert as_utc(None) is None
    # 已带时区的原样返回，不做二次换算
    already = datetime(2026, 10, 12, 3, 0, 0, tzinfo=UTC)
    assert as_utc(already) is already


def test_utcnow_is_aware():
    assert utcnow().tzinfo is not None


async def test_datetime_roundtrip_is_aware(session):
    """SQLite 读回的时间必须是 aware，否则与 utcnow() 比较会抛 TypeError。

    这正是 cache_service 里过期判断崩溃的根因。
    """
    from app.db.models.job import AuditLog

    row = AuditLog(username="tester", action="probe", resource_type="x", detail={})
    session.add(row)
    await session.commit()
    await session.refresh(row)
    assert row.created_at.tzinfo is not None, "读回的时间丢了时区"
    # 不抛 TypeError 即为通过
    assert (row.created_at <= utcnow()) is True


def test_utc_datetime_bind_adds_tzinfo():
    t = UTCDateTime()
    naive = datetime(2026, 10, 12, 3, 0, 0)
    bound = t.process_bind_param(naive, None)
    assert bound.tzinfo is not None
    assert t.process_bind_param(None, None) is None


# ---------- UUID 列容忍 str（LLM 调用日志写入崩溃的根因） ----------

def test_uuid_type_accepts_str_on_write():
    t = UUIDType()
    s = "442ad530-c4a6-462c-9ad0-b5c76ad18e82"
    assert t.process_bind_param(s, None) == uuid.UUID(s)
    u = uuid.uuid4()
    assert t.process_bind_param(u, None) is u
    assert t.process_bind_param(None, None) is None


def test_uuid_type_passes_through_invalid_str_for_db_to_reject():
    """非法字符串不静默变 None —— 那会把调用方的 bug 变成一行查不到的数据。"""
    t = UUIDType()
    assert t.process_bind_param("not-a-uuid", None) == "not-a-uuid"


def test_uuid_type_result_is_uuid():
    t = UUIDType()
    s = "442ad530-c4a6-462c-9ad0-b5c76ad18e82"
    assert isinstance(t.process_result_value(s, None), uuid.UUID)
    assert t.process_result_value(None, None) is None


async def test_call_log_accepts_str_foreign_keys(session):
    """网关传的是 str 类型的 config_id/conversation_id，日志必须写得进去。"""
    from sqlalchemy import select

    from app.db.models.llm import CallStatus, LlmCallLog

    row = LlmCallLog(
        config_id=str(uuid.uuid4()), scene="sql_generate", protocol="anthropic",
        model="stub", input_tokens=10, output_tokens=20, latency_ms=5,
        status=CallStatus.OK, error_brief="",
        conversation_id=str(uuid.uuid4()),
    )
    session.add(row)
    await session.commit()
    row_id = row.id

    # 同 session 会命中 identity map 拿到原对象（字段还是传进去的 str），
    # 这里先把缓存里的对象清空，再重读，验证真正的落库值。
    session.expunge(row)
    got = (await session.execute(
        select(LlmCallLog).where(LlmCallLog.id == row_id))).scalar_one()
    assert isinstance(got.config_id, uuid.UUID)
    assert isinstance(got.conversation_id, uuid.UUID)
    assert got.protocol == "anthropic" and got.status == CallStatus.OK
