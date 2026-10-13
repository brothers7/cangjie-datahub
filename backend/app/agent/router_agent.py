"""路由决策（PRD 2.4）：意图分类。规则优先，LLM 兜底。"""
from __future__ import annotations

import re

from app.db.models.llm import LlmScene
from app.llm.gateway import gateway
from app.llm.types import ChatMessage

INTENTS = ("metric_query", "trend", "compare", "ranking", "share", "drill_down",
           "table_qa", "etl_design", "smalltalk", "external")

_RULES: list[tuple[str, re.Pattern]] = [
    ("external", re.compile(r"(行业|市场)(平均|水平|基准|对标)|政策|友商|竞对|外部数据|行业报告", re.I)),
    ("smalltalk", re.compile(r"^(你好|您好|hi|hello|在吗|谢谢|你是谁|介绍.*自己|你能做什么|帮我什么)", re.I)),
    # ETL 开发助手（PRD 3.3B）：先于库表答疑，「建一张 xx 汇总表」是开发需求而非答疑
    ("etl_design", re.compile(
        r"(新建|创建|开发|搭建|建|做)(一)?(张|个|套)?.{0,18}(汇总表|宽表|明细表|维度表|日报表|快照表|拉链表|报表|表)|"
        r"(新建|创建|开发|搭建).{0,12}表|"
        r"etl|数据加工|加工需求|离线任务|调度任务|数据开发|"
        r"写.{0,4}(ddl|建表语句)|insert\s+overwrite", re.I)),
    # 库表与指标答疑（PRD 3.3/3.3A）：表用途/选表/字段释义/关联/血缘/指标口径/指标查表引导
    ("table_qa", re.compile(
        r"哪张表|哪些表|用哪(张|个)|用什么表|有没有.{0,6}表|"
        r"(这|那|该).{0,2}张?表.{0,6}(用途|含义|干嘛|干什么|做什么|意思|用法|怎么用)|"
        r"表.{0,4}(用途|含义|是什么意思|干什么)|字段.{0,4}(含义|意思|是什么|枚举)|"
        r"怎么(关联|join)|如何(关联|join)|join.{0,6}(键|条件|关联)|关联键|"
        r"血缘|来自哪|来源于哪|怎么来的|哪来的|上游|下游.{0,4}表|"
        r"(使用|查询|使用上).{0,4}(建议|注意)|分区(字段|键|裁剪)|怎么选表|"
        # 指标口径问答与指标查表引导（PRD 3.3-7）
        r"口径|定义|计算公式|怎么计算|怎么算|如何算|分子|分母|"
        r"(在|到|从|去)哪(张|个|些).{0,2}表|哪里查|在哪查|从哪查|"
        r"指标.{0,6}(在|到|查|含义|说明|解释)", re.I)),
    ("compare", re.compile(r"同比|环比|对比|比上月|比上周|比上期|增长多少|下降多少|变化", re.I)),
    ("ranking", re.compile(r"top\s*\d+|排行|排名|前\d+|最高|最低|最大|最小|榜单", re.I)),
    ("share", re.compile(r"占比|份额|比例|分布|构成|结构", re.I)),
    ("trend", re.compile(r"趋势|走势|近\d+天|最近\d+天|曲线|变化情况|每日|每周|每月", re.I)),
    ("drill_down", re.compile(r"按|分|拆|下钻|细分|各(地区|城市|渠道|品类|产品)", re.I)),
    ("metric_query", re.compile(r"(多少|多少|总量|总计|合计|汇总|是多少|查询|看看)", re.I)),
]

ROUTE_PROMPT = """判断用户问题意图，只输出以下标签之一，不要解释：
metric_query(查指标值) trend(趋势) compare(同比环比对比) ranking(排行) share(占比分布)
drill_down(维度拆解) table_qa(库表用途/选表/字段含义/表关联/指标口径/指标在哪张表查等答疑，不需要查数)
etl_design(新建表/数据加工/ETL开发需求) smalltalk(闲聊问候) external(需要外部行业/政策数据)
注意：「XX指标口径是什么/怎么算」「XX指标到哪张表查」属于 table_qa，不是 metric_query。"""


def route_by_rules(question: str) -> tuple[str, float]:
    q = question.strip()
    for intent, pat in _RULES:
        if pat.search(q):
            return intent, 0.9
    if q:
        return "metric_query", 0.5
    return "smalltalk", 0.9


async def route_intent(question: str, override_config_id: str | None = None,
                       custom_skills: list | None = None) -> tuple[str, float]:
    """意图路由：规则优先，LLM 兜底。

    custom_skills: 已启用的自定义 Skill 列表（有 trigger_keywords / name / description）。
    关键词命中直接返回 ("custom_skill:<name>", 0.95)；LLM 兜底时 Skill 描述注入提示词。
    """
    from app.services.custom_skills import match_by_keywords

    if custom_skills:
        hit = match_by_keywords(custom_skills, question)
        if hit is not None:
            return f"custom_skill:{hit.name}", 0.95

    intent, conf = route_by_rules(question)
    if conf >= 0.8:
        return intent, conf
    try:
        prompt = ROUTE_PROMPT
        if custom_skills:
            extra = " ".join(
                f"custom_skill:{s.name}({s.description or '、'.join((s.trigger_keywords or [])[:3])})"
                for s in custom_skills[:10])
            prompt += f"\n此外可用的自定义技能：{extra}（命中时输出 custom_skill:技能名）"
        resp = await gateway.chat(LlmScene.INTENT_ROUTE,
                                  [ChatMessage("system", prompt),
                                   ChatMessage("user", question[:300])],
                                  temperature=0.0, max_tokens=32,
                                  override_config_id=override_config_id)
        label = resp.text.strip().strip("`\"' ").lower()
        if label.startswith("custom_skill:") and custom_skills:
            name = label.split(":", 1)[1].strip()
            if any(s.name.lower() == name for s in custom_skills):
                return f"custom_skill:{name}", 0.7
        for it in INTENTS:
            if it in label:
                return it, 0.75
    except Exception:
        pass
    return intent, conf
