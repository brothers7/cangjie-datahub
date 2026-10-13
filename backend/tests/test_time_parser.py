"""时间解析：相对日、近 N 天、同比/环比周期、显式区间。"""
from __future__ import annotations

from datetime import date

from app.agent.nlu.time_parser import TimeRange, parse_time_range

TODAY = date(2026, 10, 12)  # 周一


def p(q: str) -> TimeRange | None:
    return parse_time_range(q, today=TODAY)


class TestRelativeDays:
    def test_today_yesterday(self):
        assert p("今日GMV").start == "2026-10-12"
        assert p("昨日GMV").start == "2026-10-11"
        assert p("前天订单量").start == "2026-10-10"

    def test_explicit_date(self):
        r = p("10月1日的GMV")
        assert r is not None and r.start == "2026-10-01"


class TestRecentN:
    def test_recent_7_days(self):
        r = p("近7天GMV趋势")
        assert r is not None and r.days == 7 and r.end == "2026-10-12"

    def test_recent_30_days(self):
        r = p("最近30天销售")
        assert r is not None and r.days == 30

    def test_chinese_numeral(self):
        r = p("近三个月GMV")
        assert r is not None and r.days > 80


class TestPeriods:
    def test_prev_period(self):
        from datetime import timedelta
        r = p("近7天GMV")
        prev = r.prev_period()
        assert prev.days == 7
        assert prev.end == (date.fromisoformat(r.start) - timedelta(days=1)).isoformat()

    def test_yoy_period(self):
        r = p("本月GMV")
        yoy = r.yoy_period()
        assert yoy is not None

    def test_explicit_range(self):
        r = p("2026-10-01到2026-10-07的GMV")
        assert r is not None and r.start == "2026-10-01" and r.end == "2026-10-07"
