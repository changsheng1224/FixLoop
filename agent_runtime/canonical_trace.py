"""Canonical Trace：统一事件信封、Span 上下文与校验/排序。

schema_version=1。与 ADR-009 JSONL 追加兼容：保留 ``event``/``created_at``，
并增加 ``event_type``/``timestamp``/``run_id``/``trace_id``/``span_id``/
``parent_span_id``/``status``/``seq``。
"""

from __future__ import annotations

import threading
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = "1"

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"
STATUS_UNSET = "unset"
STATUSES = frozenset({STATUS_OK, STATUS_ERROR, STATUS_CANCELLED, STATUS_UNSET})

# 文档化事件目录（既有名，不强制改名）
EVENT_CATALOG: dict[str, tuple[str, ...]] = {
    "model": ("model_request_start", "model_first_token", "model_complete"),
    "tool": ("tool_executed", "tool_preview", "tool_order_warning"),
    "skill": ("skill_matched", "skill_hint_rendered"),
    "context": ("context_built", "compression_triggered"),
    "state": (
        "repair_started",
        "repair_finished",
        "repair_cancelled",
        "agent_ask_started",
        "agent_ask_finished",
        "run_started",
        "run_finished",
        "run_cancelled",
        "span_closed",
    ),
    "artifact": ("baseline_verify_finished", "blackboard_snapshot"),
}

ENVELOPE_REQUIRED = (
    "schema_version",
    "run_id",
    "trace_id",
    "span_id",
    "event_type",
    "event",
    "timestamp",
    "created_at",
    "status",
    "seq",
)

_seq_lock = threading.Lock()
_seq_counters: dict[str, int] = {}


def next_seq(run_id: str) -> int:
    """每个 run_id 单调递增的序号（同毫秒可排序）。"""
    key = run_id or "_anonymous"
    with _seq_lock:
        n = _seq_counters.get(key, 0) + 1
        _seq_counters[key] = n
        return n


def reset_seq(run_id: str | None = None) -> None:
    with _seq_lock:
        if run_id is None:
            _seq_counters.clear()
        else:
            _seq_counters.pop(run_id, None)


def new_span_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass(frozen=True)
class SpanFrame:
    span_id: str
    parent_span_id: str | None
    name: str


_span_stack: ContextVar[tuple[SpanFrame, ...]] = ContextVar(
    "fixloop_trace_span_stack",
    default=(),
)


class TraceSpanContext:
    """基于 ContextVar 的 Span 栈。"""

    @staticmethod
    def depth() -> int:
        return len(_span_stack.get())

    @staticmethod
    def current() -> SpanFrame | None:
        stack = _span_stack.get()
        return stack[-1] if stack else None

    @staticmethod
    def push(name: str) -> SpanFrame:
        stack = _span_stack.get()
        parent = stack[-1].span_id if stack else None
        frame = SpanFrame(span_id=new_span_id(), parent_span_id=parent, name=name)
        _span_stack.set(stack + (frame,))
        return frame

    @staticmethod
    def pop() -> SpanFrame | None:
        stack = _span_stack.get()
        if not stack:
            return None
        frame = stack[-1]
        _span_stack.set(stack[:-1])
        return frame

    @staticmethod
    def reset() -> tuple[SpanFrame, ...]:
        """清空栈，返回被丢弃的 frames（根在前）。"""
        stack = _span_stack.get()
        _span_stack.set(())
        return stack

    @staticmethod
    def snapshot() -> tuple[SpanFrame, ...]:
        return _span_stack.get()


def infer_status(event: str, payload: dict[str, Any] | None = None) -> str:
    """从事件名与 payload 推断 envelope status。"""
    data = payload or {}
    if event in ("repair_cancelled", "run_cancelled"):
        return STATUS_CANCELLED
    if event in ("repair_started", "run_started", "agent_ask_started"):
        return STATUS_OK
    if event == "span_closed":
        reason = str(data.get("reason") or "")
        if reason == "abnormal":
            return STATUS_ERROR
        return STATUS_OK
    if event in ("repair_finished", "run_finished", "agent_ask_finished"):
        st = str(data.get("status") or data.get("stop_reason") or "").lower()
        if st in ("cancelled", "cancel"):
            return STATUS_CANCELLED
        if st in ("failed", "error", "exception", "timeout"):
            return STATUS_ERROR
        return STATUS_OK
    explicit = data.get("canonical_status")
    if explicit in STATUSES:
        return str(explicit)
    return STATUS_UNSET


def enrich_record(
    *,
    run_id: str,
    event: str,
    created_at: str,
    payload: dict[str, Any] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """构造带 Canonical 信封的 JSONL 记录（保留旧字段）。"""
    frame = TraceSpanContext.current()
    span_id = frame.span_id if frame else new_span_id()
    parent_span_id = frame.parent_span_id if frame else None
    seq = next_seq(run_id)
    st = status if status in STATUSES else infer_status(event, payload)
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "trace_id": run_id,  # v1: trace_id == run_id
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "event": event,
        "event_type": event,
        "timestamp": created_at,
        "created_at": created_at,
        "status": st,
        "seq": seq,
    }
    if payload:
        record["payload"] = payload
    return record


def validate_event(record: dict[str, Any], *, require_canonical: bool = True) -> list[str]:
    """校验事件；返回问题列表（空=通过）。

    旧格式（无 schema_version）在 require_canonical=False 时只检查 event/created_at。
    """
    issues: list[str] = []
    if not record.get("event") and not record.get("event_type"):
        issues.append("missing event/event_type")
    if not record.get("created_at") and not record.get("timestamp"):
        issues.append("missing created_at/timestamp")
    if not require_canonical and record.get("schema_version") is None:
        return issues
    for key in ENVELOPE_REQUIRED:
        if key == "parent_span_id":
            continue  # 允许 null
        if key not in record:
            issues.append(f"missing {key}")
    if "parent_span_id" not in record:
        issues.append("missing parent_span_id")
    sv = record.get("schema_version")
    if sv is not None and str(sv) != SCHEMA_VERSION:
        issues.append(f"unexpected schema_version={sv}")
    st = record.get("status")
    if st is not None and st not in STATUSES:
        issues.append(f"invalid status={st}")
    if record.get("event") and record.get("event_type") and record["event"] != record["event_type"]:
        issues.append("event/event_type mismatch")
    return issues


def order_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 timestamp + seq 稳定排序；无 seq 时保持相对次序（用原下标）。"""

    def key(item: tuple[int, dict[str, Any]]) -> tuple:
        idx, ev = item
        ts = str(ev.get("timestamp") or ev.get("created_at") or "")
        seq = ev.get("seq")
        try:
            seq_n = int(seq) if seq is not None else idx
        except (TypeError, ValueError):
            seq_n = idx
        return (ts, seq_n, idx)

    indexed = list(enumerate(events))
    indexed.sort(key=key)
    return [ev for _, ev in indexed]


def build_span_tree(events: list[dict[str, Any]]) -> dict[str, list[str]]:
    """span_id -> child span_ids（按首次出现顺序）。"""
    children: dict[str, list[str]] = {}
    seen: set[str] = set()
    for ev in order_events(events):
        sid = ev.get("span_id")
        pid = ev.get("parent_span_id")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        if pid:
            children.setdefault(str(pid), []).append(str(sid))
        else:
            children.setdefault("", []).append(str(sid))
    return children
