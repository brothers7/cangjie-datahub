"""中文时间表达解析：今日/昨日/本周/上周/本月/上月/最近N天/指定日期段。"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from datetime import date, timedelta

RELATIVE_DAYS = {
    "今天": 0, "今日": 0, "昨日": -1, "昨天": -1, "前天": -2,
}

WEEKDAY_WORDS = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6, "周天": 6}

DATE_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?")
# 无年份的 M月D日：补当前年，若在未来则回退到去年（口语默认最近的一次）
DATE_MD_RE = re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})日?(?!\d)")
RANGE_RE = re.compile(
    r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})\s*(?:到|至|~|—|—|-)\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})"
)
RECENT_RE = re.compile(r"(?:最近|近|过去)(\d+)(?:天|日)")
RECENT_CN_RE = re.compile(r"(?:最近|近|过去)(一|两|二|三|四|五|六|七|八|九|十|半)(天|日|周|个月|月)")
CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
          "八": 8, "九": 9, "十": 10, "半": 15}


@dataclass
class TimeRange:
    start: str
    end: str
    label: str
    grain: str = "day"  # day / week / month

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def days(self) -> int:
        s = date.fromisoformat(self.start)
        e = date.fromisoformat(self.end)
        return (e - s).days + 1

    def prev_period(self) -> "TimeRange":
        """上一个等长周期（环比）。"""
        s = date.fromisoformat(self.start)
        e = date.fromisoformat(self.end)
        n = self.days
        return TimeRange((s - timedelta(days=n)).isoformat(), (s - timedelta(days=1)).isoformat(),
                         f"上一周期({self.label})", self.grain)

    def yoy_period(self) -> "TimeRange":
        """去年同期（同比）。"""
        try:
            s = date.fromisoformat(self.start).replace(year=date.fromisoformat(self.start).year - 1)
            e = date.fromisoformat(self.end).replace(year=date.fromisoformat(self.end).year - 1)
        except ValueError:  # 2/29
            s = date.fromisoformat(self.start).replace(year=date.fromisoformat(self.start).year - 1, day=28)
            e = date.fromisoformat(self.end).replace(year=date.fromisoformat(self.end).year - 1, day=28)
        return TimeRange(s.isoformat(), e.isoformat(), f"去年同期({self.label})", self.grain)


def parse_time_range(question: str, today: date | None = None) -> TimeRange | None:
    """从问题中解析时间范围；未命中返回 None（由管线默认最近 7 天并提示）。"""
    today = today or date.today()
    q = question.strip()

    # 显式日期段
    m = RANGE_RE.search(q)
    if m:
        try:
            s = _parse_date_str(m.group(1))
            e = _parse_date_str(m.group(2))
            if s and e and s <= e:
                grain = "month" if (e - s).days > 92 else ("week" if (e - s).days > 14 else "day")
                return TimeRange(s.isoformat(), e.isoformat(), f"{s}~{e}", grain)
        except Exception:
            pass

    # 单日：今日/昨日/前天/具体日期/上/本周X
    for word, delta in RELATIVE_DAYS.items():
        if word in q:
            d = today + timedelta(days=delta)
            return TimeRange(d.isoformat(), d.isoformat(), word)
    m = DATE_RE.search(q)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return TimeRange(d.isoformat(), d.isoformat(), d.isoformat())
        except ValueError:
            pass
    m = DATE_MD_RE.search(q)   # 「10月1日」这类无年份写法
    if m:
        try:
            d = date(today.year, int(m.group(1)), int(m.group(2)))
            if d > today:
                d = date(today.year - 1, int(m.group(1)), int(m.group(2)))
            return TimeRange(d.isoformat(), d.isoformat(), d.isoformat())
        except ValueError:
            pass
    for word, wd in WEEKDAY_WORDS.items():
        if f"上{word}" in q or f"上周{word}" in q:
            days_ago = (today.weekday() - wd) % 7
            days_ago = days_ago + 7 if days_ago == 0 else days_ago
            d = today - timedelta(days=days_ago)
            return TimeRange(d.isoformat(), d.isoformat(), f"上{word}")

    # 最近 N 天
    m = RECENT_RE.search(q)
    if m:
        n = int(m.group(1))
        return _recent(today, n, f"最近{n}天")
    m = RECENT_CN_RE.search(q)
    if m:
        word, unit = m.group(1), m.group(2)
        num = CN_NUM[word]
        if unit in ("周",):
            return _recent(today, num * 7, f"最近{m.group(1)}{unit}")
        if unit in ("个月", "月"):
            if word == "半":   # 近半月 ≈ 15 天
                return _recent(today, 15, "最近半个月")
            s = _months_ago(today, num)
            return TimeRange(s.isoformat(), (today - timedelta(days=1)).isoformat(),
                             f"最近{m.group(1)}个月", "month")
        return _recent(today, num, f"最近{m.group(1)}天")

    if "本周" in q or "这周" in q or "这一周" in q:
        s = today - timedelta(days=today.weekday())
        return TimeRange(s.isoformat(), today.isoformat(), "本周")
    if "上周" in q:
        s = today - timedelta(days=today.weekday() + 7)
        return TimeRange(s.isoformat(), (s + timedelta(days=6)).isoformat(), "上周")
    if "本月" in q or "这个月" in q:
        s = today.replace(day=1)
        return TimeRange(s.isoformat(), today.isoformat(), "本月", "month")
    if "上月" in q or "上个月" in q:
        s = _months_ago(today, 1).replace(day=1)
        e = (s + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        return TimeRange(s.isoformat(), e.isoformat(), "上月", "month")
    if "今年" in q:
        s = today.replace(month=1, day=1)
        return TimeRange(s.isoformat(), today.isoformat(), "今年", "month")
    if "昨天" in q or "昨日" in q:
        d = today - timedelta(days=1)
        return TimeRange(d.isoformat(), d.isoformat(), "昨日")
    if "今天" in q or "今日" in q:
        return TimeRange(today.isoformat(), today.isoformat(), "今日")
    return None


def _recent(today: date, n: int, label: str) -> TimeRange:
    """最近 N 天：含今天。"""
    return TimeRange((today - timedelta(days=n - 1)).isoformat(), today.isoformat(), label)


def _months_ago(today: date, n: int) -> date:
    month = today.month - n
    year = today.year
    while month <= 0:
        month += 12
        year -= 1
    day = min(today.day, [31, 29 if _leap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def _leap(y: int) -> bool:
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


def _parse_date_str(s: str) -> date | None:
    s = s.replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    parts = s.split("-")
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        return None


# 问题中的对比意图词
YOY_WORDS = ("同比", "去年同")
MOM_WORDS = ("环比", "上期", "上一周期", "比上")
