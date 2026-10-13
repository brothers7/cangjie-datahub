"""图表场景匹配（PRD 8）与 SQL 守卫（PRD 7 / 13）的回归测试。"""
from __future__ import annotations

import pytest

from app.agent.skills.chart import build_chart_spec
from app.agent.skills.sql_guard import detect_injection, guard_sql

# ---------- 图表场景匹配 ----------

TREND_ROWS = [{"stat_date": f"2026-10-{d:02d}", "gmv": 100 + d} for d in range(1, 8)]
RANK_ROWS = [{"channel_name": f"渠道{i}", "gmv": 1000 - i * 50} for i in range(6)]
# LLM 经常给列起中文别名，物理列名对不上时不能退化成明细表
RANK_ALIASED = [{"渠道": f"渠道{i}", "gmv": 1000 - i * 50} for i in range(6)]
POINT_ROWS = [{"gmv": 8412.35}]


def _spec(intent, columns, rows, metric_cols, dim_cols=(), time_col=None, **kw):
    return build_chart_spec(title="t", intent=intent, columns=list(columns), rows=rows,
                            metric_cols=list(metric_cols), dim_cols=list(dim_cols),
                            time_col=time_col, **kw)


def test_trend_renders_line():
    s = _spec("trend", ["stat_date", "gmv"], TREND_ROWS, ["gmv"], time_col="stat_date")
    assert s.type == "line"
    assert s.x == [r["stat_date"] for r in TREND_ROWS]
    assert s.series[0]["name"] == "gmv"
    assert len(s.series[0]["data"]) == 7


def test_compare_renders_line_with_previous_period_series():
    prev = [{"stat_date": "2026-09-30", "gmv": 50}]
    s = _spec("compare", ["stat_date", "gmv"], TREND_ROWS, ["gmv"], time_col="stat_date",
              compare={"label": "上一周期", "rows": prev})
    assert s.type == "line"
    assert any("上一周期" in x["name"] for x in s.series)
    assert s.compare_label == "上一周期"


def test_ranking_renders_horizontal_bar():
    s = _spec("ranking", ["channel_name", "gmv"], RANK_ROWS, ["gmv"], ["channel_name"])
    assert s.type == "hbar"
    assert len(s.x) == 6


def test_ranking_works_when_llm_aliases_the_dimension_column():
    """真实链路里 SQL 常写成 channel_name AS 渠道，必须回退到第一个非指标列。"""
    s = _spec("ranking", ["渠道", "gmv"], RANK_ALIASED, ["gmv"], ["channel_name"])
    assert s.type == "hbar", "中文别名维度列应仍能渲染排行图"
    assert len(s.x) == 6


def test_share_renders_pie():
    s = _spec("share", ["region_name", "gmv"], RANK_ALIASED, ["gmv"], ["region_name"])
    assert s.type == "pie"
    assert s.pies and s.pies[0]["share"] is not None


def test_single_point_metric_renders_stat_card():
    s = _spec("metric_query", ["gmv"], POINT_ROWS, ["gmv"])
    assert s.type == "stat_card"
    assert s.cards[0]["label"] == "gmv"


def test_stat_card_shows_previous_period_delta():
    s = _spec("metric_query", ["gmv"], [{"gmv": 110}], ["gmv"],
              compare={"label": "上日", "rows": [{"gmv": 100}]})
    card = s.cards[0]
    assert card["prev"] == 100 and card["pct"] == 10.0 and card["trend"] == "up"


def test_unmatched_scenario_degrades_to_detail_table():
    """分不出场景时必须给明细表，不能给空白图。"""
    rows = [{"a": 1, "b": 2, "c": 3}] * 10
    s = _spec("unknown_intent", ["a", "b", "c"], rows, [])
    assert s.type == "table"
    assert s.columns == ["a", "b", "c"]
    assert len(s.rows) == 10


def test_time_series_without_date_values_does_not_render_line():
    """时间列存在但值不是日期（如 'all'）时不应画折线，否则 x 轴是一条直线。"""
    s = _spec("trend", ["stat_date", "gmv"], [{"stat_date": "all", "gmv": 1}],
              ["gmv"], time_col="stat_date")
    assert s.type != "line"


def test_anomalies_are_attached_to_spec():
    anomalies = [{"x": "2026-10-03", "value": 120, "direction": "up", "severity": "high"}]
    s = _spec("trend", ["stat_date", "gmv"], TREND_ROWS, ["gmv"],
              time_col="stat_date", anomalies=anomalies)
    assert s.anomalies == anomalies
    assert s.to_dict()["anomalies"] == anomalies


def test_chart_spec_is_json_serializable():
    import json

    s = _spec("ranking", ["channel_name", "gmv"], RANK_ROWS, ["gmv"], ["channel_name"])
    json.dumps(s.to_dict())  # 不抛即通过（SSE 要按 JSON 下发）


def test_ranking_works_when_llm_aliases_the_metric_column():
    """SUM(refund_amt) AS m —— 指标物理列对不上时要认别名列，否则图是一排 None。"""
    rows = [{"地区": f"区域{i}", "m": 100 - i} for i in range(5)]
    s = _spec("ranking", ["地区", "m"], rows, ["refund_amt"], ["地区"])
    assert s.type == "hbar"
    assert s.series[0]["data"] == [r["m"] for r in rows]


def test_metric_alias_fallback_excludes_dimension_and_time_columns():
    """兜底认指标列时不能把维度/时间列（文本）算成指标。"""
    rows = [{"region_name": f"R{i}", "order_id": f"SO{i}", "v": i} for i in range(5)]
    s = _spec("ranking", ["region_name", "order_id", "v"], rows, ["refund_amt"], ["region_name"])
    assert s.series[0]["name"] == "v", s.series


# ---------- SQL 守卫 ----------

ALLOWED = {"dws_trade_daily", "dim_channel"}


def test_guard_accepts_plain_select_and_injects_limit():
    r = guard_sql("SELECT stat_date, SUM(gmv) FROM dws_trade_daily GROUP BY stat_date",
                  "sqlite", ALLOWED, max_rows=100)
    assert r.ok, r.violations
    assert "LIMIT 100" in r.sql.upper()
    assert r.tables == ["dws_trade_daily"]


def test_guard_accepts_join_and_cte():
    """CTE 名会被解析成 Table 节点，不能当成未授权表（否则合法的 WITH 查询被拦）。"""
    sql = ("WITH t AS (SELECT channel_id, SUM(gmv) AS g FROM dws_trade_daily GROUP BY channel_id) "
           "SELECT c.channel_name, t.g FROM t JOIN dim_channel c ON c.channel_id = t.channel_id")
    r = guard_sql(sql, "sqlite", ALLOWED)
    assert r.ok, r.violations
    assert set(r.tables) == ALLOWED


@pytest.mark.parametrize("sql,keyword", [
    ("DELETE FROM dws_trade_daily", "DELETE"),
    ("UPDATE dws_trade_daily SET gmv = 0", "UPDATE"),
    ("DROP TABLE dws_trade_daily", "DROP"),
    ("INSERT INTO dws_trade_daily VALUES (1)", "INSERT"),
    ("TRUNCATE TABLE dws_trade_daily", "TRUNCATE"),
    ("ALTER TABLE dws_trade_daily ADD COLUMN x INT", "ALTER"),
    ("SELECT 1; GRANT ALL ON x TO y", "GRANT"),
])
def test_guard_blocks_write_statements(sql, keyword):
    r = guard_sql(sql, "sqlite", ALLOWED)
    assert not r.ok
    assert any(keyword in v.upper() for v in r.violations), r.violations


def test_guard_blocks_unauthorized_table():
    """越权读其他表必须拦死（PRD 13 无越权泄露）。"""
    r = guard_sql("SELECT * FROM secret_salary", "sqlite", ALLOWED)
    assert not r.ok
    assert any("未授权" in v or "未知的表" in v for v in r.violations)


def test_guard_blocks_stacked_statements():
    r = guard_sql("SELECT 1 FROM dws_trade_daily; SELECT 2 FROM dim_channel", "sqlite", ALLOWED)
    assert not r.ok
    assert any("只能包含一条语句" in v for v in r.violations)


def test_guard_blocks_dangerous_functions():
    r = guard_sql("SELECT pg_read_file('/etc/passwd') FROM dws_trade_daily", "sqlite", ALLOWED)
    assert not r.ok


def test_guard_blocks_select_into():
    r = guard_sql("SELECT * INTO backup FROM dws_trade_daily", "sqlite", ALLOWED)
    assert not r.ok


def test_guard_rejects_write_written_as_subquery():
    r = guard_sql("SELECT * FROM (DELETE FROM dws_trade_daily RETURNING *) x",
                  "sqlite", ALLOWED)
    assert not r.ok


def test_guard_respects_existing_smaller_limit():
    r = guard_sql("SELECT gmv FROM dws_trade_daily LIMIT 5", "sqlite", ALLOWED, max_rows=1000)
    assert r.ok
    assert "LIMIT 5" in r.sql.upper()


def test_guard_lowers_oversized_limit():
    r = guard_sql("SELECT gmv FROM dws_trade_daily LIMIT 999999", "sqlite", ALLOWED, max_rows=100)
    assert r.ok
    assert "LIMIT 100" in r.sql.upper()


def test_guard_keeps_unquoted_identifiers_readable():
    """LLM 生成的通用 SQL 基本不带引号，转换后也不该被强行加引号。"""
    r = guard_sql("SELECT gmv FROM dws_trade_daily", "mysql", ALLOWED)
    assert r.ok, r.violations
    assert r.sql.strip().endswith("LIMIT 10000")
    assert "`gmv`" not in r.sql and '"gmv"' not in r.sql


@pytest.mark.parametrize("dialect,quote", [("mysql", "`"), ("hive", "`"),
                                           ("sqlite", '"'), ("postgresql", '"')])
def test_guard_converts_dialect_quoting(dialect, quote):
    """通用 SQL 里的双引号标识符要按目标方言转成对应的引号。"""
    r = guard_sql('SELECT "gmv" FROM dws_trade_daily', dialect, ALLOWED)
    assert r.ok, r.violations
    assert f"{quote}gmv{quote}" in r.sql


def test_guard_reports_unparsable_sql():
    r = guard_sql("SELEKT broken FROM", "sqlite", ALLOWED)
    assert not r.ok


def test_guard_normalizes_table_name_case():
    r = guard_sql("SELECT gmv FROM DWS_TRADE_DAILY", "sqlite", ALLOWED)
    assert r.ok, r.violations


@pytest.mark.parametrize("text", [
    "' OR '1'='1", "1; DROP TABLE users", "a UNION SELECT password FROM users",
    "/* comment */ hidden", "xp_cmdshell('whoami')", "WAITFOR DELAY '0:0:10'",
])
def test_injection_detection_flags_attacks(text):
    assert detect_injection(text) is not None


@pytest.mark.parametrize("text", [
    "最近7天GMV趋势", "各渠道GMV占比", "本月订单量排行TOP10", "去年同期退款金额",
])
def test_injection_detection_allows_normal_questions(text):
    assert detect_injection(text) is None


# ---------- 时间 × 维度（跨表维度趋势） ----------

TREND_BY_DIM = [
    {"stat_date": "2026-10-01", "channel_name": "自然流量", "gmv": 100},
    {"stat_date": "2026-10-01", "channel_name": "搜索广告", "gmv": 60},
    {"stat_date": "2026-10-02", "channel_name": "自然流量", "gmv": 110},
    {"stat_date": "2026-10-02", "channel_name": "搜索广告", "gmv": 70},
]


def test_trend_grouped_by_dimension_becomes_one_series_per_value():
    """「各渠道GMV趋势」的 SQL 会同时带日期和渠道列。

    不做透视的话，指标列会被原样当作纵轴，把不同渠道的值串成一条锯齿线 ——
    看着像趋势，实际是渠道之间来回跳，量级完全失真。
    """
    s = _spec("trend", ["stat_date", "channel_name", "gmv"], TREND_BY_DIM, ["gmv"],
              ["channel_name"], time_col="stat_date")
    assert s.type == "line"
    assert s.x == ["2026-10-01", "2026-10-02"]
    names = {x["name"] for x in s.series}
    assert names == {"自然流量", "搜索广告"}, s.series
    assert next(x for x in s.series if x["name"] == "自然流量")["data"] == [100, 110]
    assert next(x for x in s.series if x["name"] == "搜索广告")["data"] == [60, 70]


def test_trend_without_dimension_keeps_single_series():
    """没有维度列的纯趋势仍是一条线，透视不能误伤。"""
    s = _spec("trend", ["stat_date", "gmv"], TREND_ROWS, ["gmv"], time_col="stat_date")
    assert s.type == "line"
    assert len(s.series) == 1 and len(s.series[0]["data"]) == 7


def test_trend_breakdown_fills_missing_points_with_none():
    """某渠道某天无数据时补 None（断点），不能顺移导致日期错位。"""
    rows = [
        {"stat_date": "2026-10-01", "channel_name": "A", "gmv": 10},
        {"stat_date": "2026-10-02", "channel_name": "B", "gmv": 20},
    ]
    s = _spec("trend", ["stat_date", "channel_name", "gmv"], rows, ["gmv"],
              ["channel_name"], time_col="stat_date")
    a = next(x for x in s.series if x["name"] == "A")
    b = next(x for x in s.series if x["name"] == "B")
    assert a["data"] == [10, None] and b["data"] == [None, 20]


def test_compare_with_breakdown_adds_dashed_previous_period_series():
    prev = [{"stat_date": "2026-09-01", "channel_name": "自然流量", "gmv": 90}]
    s = _spec("compare", ["stat_date", "channel_name", "gmv"], TREND_BY_DIM, ["gmv"],
              ["channel_name"], time_col="stat_date",
              compare={"label": "上一周期", "rows": prev})
    assert any(x.get("_dashed") and "上一周期" in x["name"] for x in s.series)


def test_compare_series_aligns_by_position_not_by_date():
    """去年同期的日期标签与本期完全不同，按日期对齐会画出一条空线。"""
    prev = [{"stat_date": f"2025-10-{d:02d}", "gmv": 50 + d} for d in range(1, 8)]
    s = _spec("compare", ["stat_date", "gmv"], TREND_ROWS, ["gmv"], time_col="stat_date",
              compare={"label": "去年同期", "rows": prev})
    dashed = next(x for x in s.series if x["name"].startswith("去年同期"))
    assert dashed["data"] == [51, 52, 53, 54, 55, 56, 57]


def test_compare_series_pads_when_previous_period_is_short():
    """对比期被保留期截断（只有 3 天）时补齐、不拉伸成 7 天。"""
    prev = [{"stat_date": f"2026-09-{d:02d}", "gmv": 10 * d} for d in range(1, 4)]
    s = _spec("compare", ["stat_date", "gmv"], TREND_ROWS, ["gmv"], time_col="stat_date",
              compare={"label": "上一周期", "rows": prev})
    dashed = next(x for x in s.series if x["name"].startswith("上一周期"))
    assert dashed["data"] == [10, 20, 30, None, None, None, None]


def test_compare_breakdown_aligns_each_dimension_by_position():
    prev = [{"stat_date": f"2025-10-{d:02d}", "channel_name": ch, "gmv": 10 * d}
            for ch in ("自然流量", "搜索广告") for d in range(1, 3)]
    s = _spec("compare", ["stat_date", "channel_name", "gmv"], TREND_BY_DIM, ["gmv"],
              ["channel_name"], time_col="stat_date",
              compare={"label": "去年同期", "rows": prev})
    a = next(x for x in s.series if x["name"] == "去年同期·自然流量")
    b = next(x for x in s.series if x["name"] == "去年同期·搜索广告")
    assert a["data"] == [10, 20] and b["data"] == [10, 20]
