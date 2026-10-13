"""字段语义分类规则引擎：指标 / 维度 / 时间 自动识别（PRD 3.2.4）。

规则优先、加分制置信度；可选 LLM 批量复核在 API 层触发。
"""
from __future__ import annotations

import re

from app.connectors.base import ColumnMeta, TableMeta
from app.db.models.metadata import AggFunc, SemanticRole, TableLayer

# ---------- 词典（中英混合，均可扩充） ----------
DIMENSION_WORDS: dict[str, str] = {
    # 英文 -> dimension_type
    "region": "geo", "province": "geo", "city": "geo", "district": "geo", "area": "geo",
    "country": "geo", "address": "geo",
    "channel": "channel", "platform": "channel", "source": "channel", "medium": "channel",
    "campaign": "channel", "utm": "channel", "terminal": "channel", "device": "channel",
    "os": "channel", "app_version": "channel", "version": "channel",
    "category": "product", "cate": "product", "product": "product", "sku": "product",
    "spu": "product", "brand": "product", "item": "product", "goods": "product",
    "dept": "dept", "department": "dept", "org": "dept", "team": "dept", "company": "dept",
    "user_type": "user_segment", "user_level": "user_segment", "member_level": "user_segment",
    "level": "user_segment", "segment": "user_segment", "gender": "user_segment",
    "age_group": "user_segment", "industry": "user_segment", "occupation": "user_segment",
    "status": "other", "state": "other", "type": "other", "flag": "other", "is_": "other",
    "pay_type": "other", "pay_method": "other", "payment": "other",
    "地": "geo", "地区": "geo", "省份": "geo", "省": "geo", "城市": "geo", "市": "geo",
    "区": "geo", "国家": "geo", "大区": "geo",
    "渠道": "channel", "平台": "channel", "来源": "channel", "终端": "channel",
    "品类": "product", "类目": "product", "商品": "product", "品牌": "product",
    "部门": "dept", "组织": "dept", "团队": "dept",
    "类型": "other", "状态": "other", "级别": "user_segment", "等级": "user_segment",
    "会员": "user_segment", "人群": "user_segment", "性别": "user_segment",
    "行业": "user_segment", "支付方式": "other", "方式": "other",
}

METRIC_WORDS = [
    # 英文
    "gmv", "sales", "revenue", "amount", "amt", "turnover", "income",
    "price", "unit_price", "cost", "fee", "profit", "margin", "discount",
    "qty", "quantity", "cnt", "count", "num", "number", "total",
    "uv", "pv", "dau", "mau", "roi", "cvr", "ctr", "rate", "ratio", "percent", "pct",
    "duration", "avg", "score", "refund", "balance", "weight",
    # 中文
    "金额", "销售额", "营收", "收入", "成交", "单价", "均价", "价格", "成本", "费用",
    "利润", "毛利", "折扣", "优惠", "数量", "次数", "笔数", "件数", "人数", "频次",
    "率", "占比", "比例", "时长", "分数", "评分", "退款", "余额", "重量", "库存",
]

TIME_NAME_PATTERNS = [
    r"^dt$", r"^date$", r"^day$", r"^month$", r"^year$", r"^stat_?date", r"^trade_?date",
    r"_date$", r"_time$", r"_at$", r"日期", r"时间",
]
# 命名强命中：即使物理类型是 TEXT/VARCHAR（SQLite、Hive 外部表等把日期存成字符串）
# 也按时间字段处理，否则时间粒度无法识别，趋势/对比/异常检测全部失效。
TIME_NAME_STRONG = [
    r"^dt$", r"^date$", r"^stat_?date", r"^trade_?date", r"_date$", r"_time$", r"_at$",
    r"日期", r"时间",
]
TIME_TYPE_PATTERNS = [r"date", r"datetime", r"timestamp"]

NUMERIC_TYPE_PATTERN = r"int|decimal|numeric|float|double|real|number|money|bigint|smallint"
ID_COLUMN_PATTERN = r"(^|_)(id|uuid|guid)$|^pk$"

LAYER_PREFIXES = {
    "ods_": TableLayer.ODS, "dwd_": TableLayer.DWD, "dws_": TableLayer.DWS,
    "ads_": TableLayer.ADS, "dim_": TableLayer.DIM, "fact_": TableLayer.DWD,
    "aggr_": TableLayer.DWS, "summary_": TableLayer.DWS,
}
LAYER_KEYWORDS = [
    ("明细", TableLayer.DWD), ("流水", TableLayer.DWD), ("快照", TableLayer.DWD),
    ("汇总", TableLayer.DWS), ("聚合", TableLayer.DWS), ("日报", TableLayer.DWS),
    ("主题", TableLayer.DWS), ("结果", TableLayer.ADS), ("指标", TableLayer.ADS),
    ("报表", TableLayer.ADS), ("维表", TableLayer.DIM), ("字典", TableLayer.DIM),
]


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def classify_table_layer(table_name: str, comment: str = "") -> TableLayer:
    name = _norm(table_name)
    for prefix, layer in LAYER_PREFIXES.items():
        if name.startswith(prefix):
            return layer
    text = f"{name} {_norm(comment)}"
    for kw, layer in LAYER_KEYWORDS:
        if kw in text:
            return layer
    return TableLayer.UNKNOWN


def _is_time_column(col: ColumnMeta) -> bool:
    name, dtype = _norm(col.column_name), _norm(col.data_type)
    if any(re.search(p, name) for p in TIME_NAME_STRONG):
        return True
    if any(re.search(p, name) for p in TIME_NAME_PATTERNS):
        return any(re.search(p, dtype) for p in TIME_TYPE_PATTERNS) or not dtype
    return any(re.search(p, dtype) for p in TIME_TYPE_PATTERNS) and any(
        re.search(p, name) for p in TIME_NAME_PATTERNS
    )


def _match_dimension(name: str, comment: str) -> str | None:
    """返回 dimension_type；未命中返回 None。"""
    text = f"{name} {comment}".lower()
    for word, dtype in DIMENSION_WORDS.items():
        if word in text:
            return dtype
    return None


def _metric_signal(name: str, comment: str) -> bool:
    text = f"{name} {comment}".lower()
    return any(w in text for w in METRIC_WORDS)


def guess_agg(col: ColumnMeta, layer: TableLayer) -> AggFunc | None:
    """按字段语义 + 表分层推断聚合函数。

    - 比率/均价类 → avg
    - DWS/ADS 中的 *_cnt/uv 等已是粒度内聚合 → 跨期用 sum
    - DWD 明细中的 uv/用户数 → count_distinct；cnt → count；金额 → sum
    """
    text = f"{col.column_name} {col.comment}".lower()
    if any(w in text for w in ("rate", "ratio", "pct", "percent", "率", "占比", "比例", "均价", "单价", "平均", "avg")):
        return AggFunc.AVG
    is_dwd = layer in (TableLayer.DWD, TableLayer.ODS)
    if re.search(r"(uv|dau|mau|用户数|访客)", text):
        return AggFunc.COUNT_DISTINCT if is_dwd else AggFunc.SUM
    if re.search(r"(cnt|count|次数|笔数|件数|单量)", text):
        return AggFunc.COUNT if is_dwd else AggFunc.SUM
    if re.search(r"(amount|amt|gmv|sales|revenue|金额|销售额|收入|成交额|总价|总额)", text):
        return AggFunc.SUM
    return AggFunc.SUM  # 数值度量兜底


def _is_numeric(col: ColumnMeta) -> bool:
    return bool(re.search(NUMERIC_TYPE_PATTERN, _norm(col.data_type)))


class ColumnClassification:
    __slots__ = ("role", "agg", "confidence", "dimension_type", "reason")

    def __init__(self, role: SemanticRole, confidence: float, agg: AggFunc | None = None,
                 dimension_type: str | None = None, reason: str = ""):
        self.role = role
        self.agg = agg
        self.confidence = confidence
        self.dimension_type = dimension_type
        self.reason = reason


def classify_column(col: ColumnMeta, layer: TableLayer,
                    stats: dict | None = None) -> ColumnClassification:
    """单字段分类。stats 为采样统计 {distinct, non_null, ratio, examples}。"""
    name = _norm(col.column_name)
    comment = _norm(col.comment)
    stats = stats or {}

    # 1) 时间字段
    if _is_time_column(col):
        return ColumnClassification(SemanticRole.TIME, 0.95, reason="时间类型/命名")

    # 主键 / 纯 ID 列不作为业务指标
    is_id = bool(re.search(ID_COLUMN_PATTERN, name)) or col.is_primary_key

    # 2) 维度：词典命中（强信号）
    dim_type = _match_dimension(name, comment)
    ratio = stats.get("ratio")
    if dim_type and not _is_numeric(col):
        conf = 0.95 if dim_type != "other" else 0.8
        return ColumnClassification(SemanticRole.DIMENSION, conf, dimension_type=dim_type,
                                    reason=f"维度词典命中({dim_type})")

    # 3) 指标：数值 + 度量词典
    if _is_numeric(col) and not col.is_primary_key and _metric_signal(name, comment):
        conf = 0.9
        if comment:
            conf = 0.95
        return ColumnClassification(SemanticRole.METRIC, conf, agg=guess_agg(col, layer),
                                    reason="数值+度量词典")

    # 4) 低基数字符串 → 维度（弱信号）
    if ratio is not None and ratio < 0.05 and not _is_numeric(col) and not is_id:
        return ColumnClassification(SemanticRole.DIMENSION, 0.7,
                                    dimension_type=_match_dimension(name, comment) or "other",
                                    reason=f"低基数比({ratio:.3f})")

    # 5) 数值非 ID → 指标（弱信号）
    if _is_numeric(col) and not is_id:
        return ColumnClassification(SemanticRole.METRIC, 0.6, agg=guess_agg(col, layer),
                                    reason="数值度量兜底")

    # 6) 有注释的外键列 → 维度
    if is_id and comment and ("id" in name or "编号" in comment):
        return ColumnClassification(SemanticRole.DIMENSION, 0.55, dimension_type="other",
                                    reason="外键引用")

    return ColumnClassification(SemanticRole.UNKNOWN, 0.3, reason="无法识别，待人工复核")


def classify_table(tm: TableMeta, stats_by_col: dict[str, dict] | None = None) -> dict[str, ColumnClassification]:
    """整表分类，返回 {column_name: classification}。"""
    layer = classify_table_layer(tm.table_name, tm.comment)
    stats_by_col = stats_by_col or {}
    return {
        c.column_name: classify_column(c, layer, stats_by_col.get(c.column_name))
        for c in tm.columns
    }
