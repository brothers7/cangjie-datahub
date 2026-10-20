"""管线上下文与 SSE 事件定义。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from app.agent.nlu.time_parser import TimeRange
from app.connectors.base import QueryResult
from app.kg.service import LinkedEntities, TableCandidate, Violation


@dataclass
class StepTrace:
    step: str
    status: str = "running"   # running / finished / failed / skipped
    detail: str = ""
    ms: int = 0
    t0: float = field(default_factory=time.monotonic)

    def finish(self, detail: str = "") -> None:
        self.status = "finished"
        self.detail = detail or self.detail
        self.ms = int((time.monotonic() - self.t0) * 1000)

    def fail(self, detail: str) -> None:
        self.status = "failed"
        self.detail = detail
        self.ms = int((time.monotonic() - self.t0) * 1000)

    def to_dict(self) -> dict:
        return {"step": self.step, "status": self.status, "detail": self.detail, "ms": self.ms}


@dataclass
class PipelineContext:
    # 输入
    user_id: str = ""
    username: str = ""
    conversation_id: str = ""
    question: str = ""
    confirm: dict | None = None      # {metric_id} 用户确认的指标
    datasource = None                # DataSource ORM
    llm_config_id: str | None = None  # 会话级模型覆盖（用户在对话窗口选的大模型）
    # 状态与记忆
    state: dict = field(default_factory=dict)
    profile: dict = field(default_factory=dict)
    # 中间产物
    intent: str = ""
    intent_conf: float = 0.0
    rewritten: str = ""
    time_range: TimeRange | None = None
    linked: LinkedEntities | None = None
    violations: list[Violation] = field(default_factory=list)
    metric_rows: list = field(default_factory=list)
    dim_rows: list = field(default_factory=list)
    table: TableCandidate | None = None
    time_col: str = ""
    sql: str = ""
    guarded_sql: str = ""
    query: QueryResult | None = None
    cleaned_rows: list = field(default_factory=list)
    validation: dict = field(default_factory=dict)
    compare: dict | None = None       # {rows, label}
    anomalies: dict = field(default_factory=dict)
    chart_spec: dict | None = None
    web_text: str = ""
    web_evidences: list = field(default_factory=list)
    answer_text: str = ""
    report: dict | None = None
    etl_design: dict | None = None      # ETL 开发助手产出（PRD 3.3B，仅生成不执行）
    metric_cards: list | None = None    # 指标口径卡（PRD 3.3-7，库表与指标答疑）
    query_record_id: str = ""
    cache_hit: bool = False
    pending_confirm: list = field(default_factory=list)  # 候选指标（confirm_required）
    # 统计
    steps: list[StepTrace] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    error: str = ""
    # 内部缓存（物理列名映射、脱敏规则）
    _column_names: dict = field(default_factory=dict, repr=False)
    _dim_column_names: dict = field(default_factory=dict, repr=False)
    _mask_rules: list = field(default_factory=list, repr=False)

    # 事件队列（SSE）
    events: asyncio.Queue = field(default_factory=asyncio.Queue, repr=False)

    def step(self, name: str, detail: str = "") -> StepTrace:
        st = StepTrace(name, detail=detail)
        self.steps.append(st)
        self.emit("step", {"step": name, "status": "started"})
        return st

    def emit(self, event: str, data: dict) -> None:
        self.events.put_nowait({"event": event, "data": data})

    def emit_step(self, st: StepTrace, detail: str = "") -> None:
        if detail:
            st.detail = detail
        self.emit("step", {"step": st.step, "status": st.status, "detail": st.detail, "ms": st.ms})

    @property
    def duration_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)


def _uuid(s):
    """宽容 UUID 转换：接受 str / UUID 两种入参。"""
    import uuid as _u
    return s if isinstance(s, _u.UUID) else _u.UUID(str(s))
