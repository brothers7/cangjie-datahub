"""SSE 事件协议（前端契约）：

meta / step / kg_match / sql / token / chart / report / confirm_required / error / done
"""
from __future__ import annotations

import json

STEP_LABELS = {
    "rewrite": "问题理解", "route": "意图识别", "memory": "记忆检索", "kg_match": "指标匹配",
    "validation": "口径校验", "route_table": "数仓路由", "sql": "SQL 生成", "guard": "安全校验",
    "query": "数据查询", "compute": "指标计算", "check": "数据校验", "anomaly": "异常检测",
    "chart": "图表生成", "web": "联网检索", "report": "报表生成", "answer": "分析解读",
}


def sse_format(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
