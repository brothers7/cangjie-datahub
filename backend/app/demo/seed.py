"""SQLite 电商演示库：DWD/DWS/ADS 分层 + 剧本化数据。

- 90 天订单明细（约 4 万行），含周末效应 + 季节趋势
- 剧本1：第 70 天起「社交媒体」渠道 GMV 连续暴跌 40%（演示异常检测）
- 剧本2：第 45 天全站大促脉冲 2.5 倍（演示同比/对比）
- SQLite 无列注释能力 → 写入 _meta_* sidecar 表由连接器合并进元数据

独立可执行：python -m app.demo.seed --path ./local/ecom_demo.db [--force]
"""
from __future__ import annotations

import argparse
import random
import sqlite3
import statistics
from datetime import date, timedelta
from pathlib import Path

REGIONS = ["华东", "华南", "华北", "西南", "华中", "东北", "西北"]
REGION_WEIGHTS = [0.30, 0.20, 0.18, 0.12, 0.10, 0.06, 0.04]

CHANNELS = ["自然流量", "搜索广告", "社交媒体", "直播带货", "私域运营", "应用商店"]
CHANNEL_WEIGHTS = [0.25, 0.22, 0.18, 0.15, 0.12, 0.08]

CATES = ["数码电器", "服饰鞋包", "美妆个护", "食品生鲜", "家居日用", "母婴玩具"]
CATE_WEIGHTS = [0.24, 0.22, 0.18, 0.14, 0.12, 0.10]

MEMBER_LEVELS = ["普通会员", "白银会员", "黄金会员", "黑金会员"]

TABLE_COMMENTS = {
    "dim_date": "日期维表",
    "dim_region": "地区维表",
    "dim_channel": "渠道维表",
    "dim_product": "商品维表",
    "dim_user": "用户维表",
    "dwd_order_detail": "交易订单明细表（订单粒度，含支付与退款）",
    "dws_trade_daily": "交易日汇总表（日×地区×渠道粒度）",
    "ads_gmv_trend": "GMV 趋势主题表（日粒度）",
    "ads_region_rank": "地区排行主题表（近30天）",
    "ads_channel_share": "渠道占比主题表（近30天）",
}

COLUMN_COMMENTS = {
    ("dim_date", "date"): "日期",
    ("dim_date", "is_weekend"): "是否周末",
    ("dim_date", "month"): "自然月",
    ("dim_region", "region_name"): "地区名称",
    ("dim_channel", "channel_name"): "渠道名称",
    ("dim_product", "cate"): "品类",
    ("dim_product", "brand"): "品牌",
    ("dim_product", "price"): "商品单价",
    ("dim_user", "is_new"): "是否新用户",
    ("dim_user", "member_level"): "会员等级",
    ("dwd_order_detail", "order_id"): "订单号",
    ("dwd_order_detail", "order_date"): "下单日期",
    ("dwd_order_detail", "user_id"): "用户ID",
    ("dwd_order_detail", "region_id"): "地区ID",
    ("dwd_order_detail", "channel_id"): "渠道ID",
    ("dwd_order_detail", "product_id"): "商品ID",
    ("dwd_order_detail", "qty"): "购买件数",
    ("dwd_order_detail", "pay_amt"): "支付金额",
    ("dwd_order_detail", "refund_amt"): "退款金额",
    ("dwd_order_detail", "order_status"): "订单状态",
    ("dws_trade_daily", "stat_date"): "统计日期",
    ("dws_trade_daily", "gmv"): "当日GMV",
    ("dws_trade_daily", "order_cnt"): "订单量",
    ("dws_trade_daily", "pay_uv"): "支付UV",
    ("dws_trade_daily", "refund_amt"): "退款金额",
    ("dws_trade_daily", "buyer_uv"): "买家数",
    ("ads_gmv_trend", "stat_date"): "统计日期",
    ("ads_gmv_trend", "gmv"): "当日GMV",
    ("ads_gmv_trend", "order_cnt"): "订单量",
    ("ads_gmv_trend", "pay_uv"): "支付UV",
    ("ads_gmv_trend", "aov"): "客单价",
    ("ads_region_rank", "region_name"): "地区名称",
    ("ads_region_rank", "gmv"): "近30天GMV",
    ("ads_region_rank", "order_cnt"): "近30天订单量",
    ("ads_channel_share", "channel_name"): "渠道名称",
    ("ads_channel_share", "gmv"): "近30天GMV",
    ("ads_channel_share", "gmv_share"): "GMV占比(%)",
}


def build_demo_db(path: str, days: int = 90, force: bool = False) -> None:
    p = Path(path)
    if p.exists() and not force:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(p) + suffix).unlink(missing_ok=True)

    rng = random.Random(20261012)
    today = date.today()
    dates = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    c = conn.cursor()

    _create_tables(c)

    # ---- 维表 ----
    c.executemany("INSERT INTO dim_date VALUES (?,?,?,?)",
                  [(d.isoformat(), int(d.weekday() >= 5), d.strftime("%Y-%m"),
                    f"Q{(d.month - 1) // 3 + 1}") for d in dates])
    c.executemany("INSERT INTO dim_region VALUES (?,?)",
                  list(enumerate(REGIONS, 1)))
    c.executemany("INSERT INTO dim_channel VALUES (?,?)",
                  list(enumerate(CHANNELS, 1)))
    products = []
    pid = 1
    brand_pool = ["星辰", "远山", "蓝鲸", "木棉", "极光", "云帆"]
    for cate in CATES:
        for _ in range(40):
            price = round(rng.uniform(29, 1999), 2)
            products.append((pid, cate, rng.choice(brand_pool), price))
            pid += 1
    c.executemany("INSERT INTO dim_product VALUES (?,?,?,?)", products)
    users = []
    for uid in range(1, 20001):
        reg_day = rng.randint(0, days + 365)
        users.append((uid, int(rng.random() < 0.25), rng.choice(MEMBER_LEVELS),
                      (today - timedelta(days=reg_day)).isoformat()))
    c.executemany("INSERT INTO dim_user VALUES (?,?,?,?)", users)

    # ---- DWD 订单明细（含剧本） ----
    price_by_pid = {p[0]: p[3] for p in products}
    orders = []
    order_seq = 100000
    social_id = CHANNELS.index("社交媒体") + 1
    for di, d in enumerate(dates):
        base = 300 + di * 2.2                       # 缓慢增长趋势
        if d.weekday() >= 5:
            base *= 1.25                            # 周末效应
        if di == 45:
            base *= 2.5                             # 大促脉冲（剧本2）
        n_orders = int(base * rng.uniform(0.92, 1.08))
        for _ in range(n_orders):
            region_id = rng.choices(range(1, len(REGIONS) + 1), weights=REGION_WEIGHTS)[0]
            channel_id = rng.choices(range(1, len(CHANNELS) + 1), weights=CHANNEL_WEIGHTS)[0]
            # 剧本1：第 70 天起社交媒体渠道量价齐跌 40%
            amt_factor = 1.0
            if channel_id == social_id and di >= 70:
                amt_factor = 0.6
            product_id = rng.randint(1, len(products))
            qty = rng.choices([1, 2, 3, 4], weights=[0.6, 0.25, 0.1, 0.05])[0]
            pay_amt = round(price_by_pid[product_id] * qty * amt_factor
                            * rng.uniform(0.9, 1.1), 2)
            status = rng.choices(
                ["已完成", "已发货", "已支付", "已退款", "待支付"],
                weights=[0.52, 0.2, 0.15, 0.08, 0.05])[0]
            refund_amt = pay_amt if status == "已退款" else 0.0
            order_seq += 1
            orders.append((f"SO{order_seq}", d.isoformat(), rng.randint(1, 20000),
                           region_id, channel_id, product_id, qty, pay_amt, refund_amt, status))
    c.executemany("INSERT INTO dwd_order_detail VALUES (?,?,?,?,?,?,?,?,?,?)", orders)

    # ---- DWS 日×地区×渠道 ----
    agg: dict[tuple, dict] = {}
    for o in orders:
        k = (o[1], o[3], o[4])
        a = agg.setdefault(k, {"gmv": 0.0, "order_cnt": 0, "users": set(), "refund": 0.0})
        if o[9] != "待支付":
            a["gmv"] += o[7]
            a["order_cnt"] += 1
            a["users"].add(o[2])
        a["refund"] += o[8]
    c.executemany(
        "INSERT INTO dws_trade_daily VALUES (?,?,?,?,?,?,?,?)",
        [(k[0], k[1], k[2], round(v["gmv"], 2), v["order_cnt"], len(v["users"]),
          round(v["refund"], 2), len(v["users"])) for k, v in sorted(agg.items())])

    # ---- ADS：趋势 / 地区排行 / 渠道占比（近30天） ----
    last30 = {(d, r, ch): v for (d, r, ch), v in agg.items()
              if date.fromisoformat(d) >= today - timedelta(days=30)}
    by_day: dict[str, dict] = {}
    for (d, _, _), v in last30.items():
        b = by_day.setdefault(d, {"gmv": 0.0, "cnt": 0, "uv": 0, "aov": []})
        b["gmv"] += v["gmv"]
        b["cnt"] += v["order_cnt"]
        b["uv"] += len(v["users"])
        b["aov"].append(v["gmv"] / max(v["order_cnt"], 1))
    c.executemany(
        "INSERT INTO ads_gmv_trend VALUES (?,?,?,?,?)",
        [(d, round(v["gmv"], 2), v["cnt"], v["uv"],
          round(statistics.mean(v["aov"]), 2)) for d, v in sorted(by_day.items())])

    by_region: dict[str, dict] = {}
    for (_, r, _), v in last30.items():
        b = by_region.setdefault(REGIONS[r - 1], {"gmv": 0.0, "cnt": 0, "uv": 0})
        b["gmv"] += v["gmv"]
        b["cnt"] += v["order_cnt"]
        b["uv"] += len(v["users"])
    c.executemany(
        "INSERT INTO ads_region_rank VALUES (?,?,?,?)",
        [(r, round(v["gmv"], 2), v["cnt"], v["uv"])
         for r, v in sorted(by_region.items(), key=lambda kv: -kv[1]["gmv"])])

    by_channel: dict[str, float] = {}
    for (_, _, ch), v in last30.items():
        by_channel[CHANNELS[ch - 1]] = by_channel.get(CHANNELS[ch - 1], 0.0) + v["gmv"]
    total_gmv = sum(by_channel.values())
    c.executemany(
        "INSERT INTO ads_channel_share VALUES (?,?,?)",
        [(ch, round(v, 2), round(v / total_gmv * 100, 2))
         for ch, v in sorted(by_channel.items(), key=lambda kv: -kv[1])])

    # ---- sidecar 注释 ----
    c.executemany("INSERT INTO _meta_table_comments VALUES (?,?)",
                  list(TABLE_COMMENTS.items()))
    c.executemany("INSERT INTO _meta_column_comments VALUES (?,?,?)",
                  [(t, col, cmt) for (t, col), cmt in COLUMN_COMMENTS.items()])

    conn.commit()
    conn.close()
    print(f"[demo-seed] demo db ready: {path}（订单 {len(orders)} 行，{days} 天）")


def _create_tables(c: sqlite3.Cursor) -> None:
    c.executescript("""
CREATE TABLE dim_date (date TEXT PRIMARY KEY, is_weekend INTEGER, month TEXT, quarter TEXT);
CREATE TABLE dim_region (region_id INTEGER PRIMARY KEY, region_name TEXT);
CREATE TABLE dim_channel (channel_id INTEGER PRIMARY KEY, channel_name TEXT);
CREATE TABLE dim_product (product_id INTEGER PRIMARY KEY, cate TEXT, brand TEXT, price REAL);
CREATE TABLE dim_user (user_id INTEGER PRIMARY KEY, is_new INTEGER, member_level TEXT, register_date TEXT);
CREATE TABLE dwd_order_detail (
    order_id TEXT PRIMARY KEY, order_date TEXT, user_id INTEGER, region_id INTEGER,
    channel_id INTEGER, product_id INTEGER, qty INTEGER, pay_amt REAL,
    refund_amt REAL, order_status TEXT);
CREATE TABLE dws_trade_daily (
    stat_date TEXT, region_id INTEGER, channel_id INTEGER, gmv REAL,
    order_cnt INTEGER, pay_uv INTEGER, refund_amt REAL, buyer_uv INTEGER);
CREATE TABLE ads_gmv_trend (stat_date TEXT PRIMARY KEY, gmv REAL, order_cnt INTEGER, pay_uv INTEGER, aov REAL);
CREATE TABLE ads_region_rank (region_name TEXT PRIMARY KEY, gmv REAL, order_cnt INTEGER, pay_uv INTEGER);
CREATE TABLE ads_channel_share (channel_name TEXT PRIMARY KEY, gmv REAL, gmv_share REAL);
CREATE INDEX idx_dwd_date ON dwd_order_detail (order_date);
CREATE INDEX idx_dwd_region ON dwd_order_detail (region_id);
CREATE INDEX idx_dwd_channel ON dwd_order_detail (channel_id);
CREATE INDEX idx_dws_date ON dws_trade_daily (stat_date);
CREATE TABLE _meta_table_comments (table_name TEXT PRIMARY KEY, comment TEXT);
CREATE TABLE _meta_column_comments (table_name TEXT, column_name TEXT, comment TEXT,
    PRIMARY KEY (table_name, column_name));
""")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="./local/ecom_demo.db")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--days", type=int, default=90)
    a = ap.parse_args()
    build_demo_db(a.path, days=a.days, force=a.force)
