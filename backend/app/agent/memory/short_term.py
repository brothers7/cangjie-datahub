"""短时记忆：会话级上下文（多轮追问不丢失，PRD 4.3）。"""
from __future__ import annotations


from app.db.models import Conversation
from app.db.models.base import utcnow


async def load_state(session, conversation_id) -> dict:
    conv = await session.get(Conversation, conversation_id)
    return dict(conv.short_term_state or {}) if conv else {}


async def save_state(session, conversation_id, state: dict) -> None:
    conv = await session.get(Conversation, conversation_id)
    if conv:
        conv.short_term_state = state
        conv.last_active_at = utcnow()


def merge_state(prev: dict, *, metric_names: list[str] | None = None,
                dim_names: list[str] | None = None, time_range: dict | None = None,
                last_sql: str | None = None, question: str | None = None,
                datasource_id: str | None = None, compare_base: str | None = None) -> dict:
    """追问时保留最近一次有效上下文（新值覆盖，None 不覆盖）。"""
    state = dict(prev)
    if metric_names:
        state["metrics"] = metric_names
    if dim_names:
        state["dimensions"] = dim_names
    if time_range:
        state["time_range"] = time_range
    if last_sql:
        state["last_sql"] = last_sql[:2000]
    if question:
        state["last_question"] = question[:500]
    if datasource_id:
        state["datasource_id"] = datasource_id
    if compare_base:
        state["compare_base"] = compare_base
    return state


def context_prompt(state: dict) -> str:
    """注入改写提示词的上下文摘要。"""
    if not state:
        return "（无历史上下文，首轮提问）"
    parts = []
    if state.get("last_question"):
        parts.append(f"上一轮问题: {state['last_question']}")
    if state.get("metrics"):
        parts.append(f"已讨论指标: {'、'.join(state['metrics'][:8])}")
    if state.get("dimensions"):
        parts.append(f"已用维度: {'、'.join(state['dimensions'][:6])}")
    if state.get("time_range"):
        tr = state["time_range"]
        parts.append(f"时间范围: {tr.get('label', '')} ({tr.get('start')}~{tr.get('end')})")
    return "\n".join(parts) or "（无历史上下文）"
