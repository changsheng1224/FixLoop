"""Canonical Trace 信封、Span 与顺序还原测试。"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from agent_runtime.canonical_trace import (
    SCHEMA_VERSION,
    STATUS_ERROR,
    STATUS_OK,
    TraceSpanContext,
    infer_status,
    order_events,
    reset_seq,
    validate_event,
)
from agent_runtime.run_store import RunStore
from src.repair.run_trace import RepairRunTracer


@pytest.fixture(autouse=True)
def _clean_span_ctx():
    TraceSpanContext.reset()
    reset_seq()
    yield
    TraceSpanContext.reset()
    reset_seq()


@pytest.fixture
def work_dir():
    """独立临时目录（避免 Windows 上全局 .pytest-tmp 清理失败）。"""
    path = Path(tempfile.mkdtemp(prefix="canonical-trace-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class TestEnvelope:
    def test_append_writes_canonical_fields(self, work_dir: Path):
        store = RunStore(str(work_dir))
        TraceSpanContext.push("root")
        store.append_trace_event("run-a", "tool_executed", {"tool": "read_file"})
        events = store.load_trace_events("run-a")
        assert len(events) == 1
        ev = events[0]
        assert not validate_event(ev)
        assert ev["schema_version"] == SCHEMA_VERSION
        assert ev["run_id"] == "run-a"
        assert ev["trace_id"] == "run-a"
        assert ev["event"] == "tool_executed"
        assert ev["event_type"] == "tool_executed"
        assert ev["created_at"] == ev["timestamp"]
        assert ev["status"] == "unset"
        assert isinstance(ev["seq"], int)
        assert ev["span_id"]
        assert "parent_span_id" in ev

    def test_order_events_by_seq(self):
        events = [
            {"timestamp": "2026-01-01T00:00:00+00:00", "seq": 2, "event": "b"},
            {"timestamp": "2026-01-01T00:00:00+00:00", "seq": 1, "event": "a"},
            {"timestamp": "2026-01-01T00:00:01+00:00", "seq": 1, "event": "c"},
        ]
        ordered = order_events(events)
        assert [e["event"] for e in ordered] == ["a", "b", "c"]


class TestSpanLifecycle:
    def test_parent_child_spans(self, work_dir: Path):
        tracer = RepairRunTracer(str(work_dir))
        run_id = tracer.begin("TypeError in calc")
        root = TraceSpanContext.current()
        assert root is not None
        assert root.parent_span_id is None
        assert TraceSpanContext.depth() == 1

        TraceSpanContext.push("ask:localizer:localize")
        child = TraceSpanContext.current()
        assert child is not None
        assert child.parent_span_id == root.span_id
        tracer.emit("localizer", "tool_executed", {"tool": "stack_parse"})
        tracer.emit("localizer", "agent_ask_finished", {"stop_reason": "final"}, status="ok")
        TraceSpanContext.pop()
        assert TraceSpanContext.current() == root
        tracer.close_dangling_ask_spans()
        tracer.emit("orchestrator", "repair_finished", {"status": "success"}, status="ok")
        tracer.end_root_span()
        events = tracer.store.load_ordered_trace(run_id)
        assert events[0]["event"] == "repair_started"
        assert events[0]["status"] == STATUS_OK
        names = [e["event"] for e in events]
        assert "repair_finished" in names
        assert "tool_executed" in names
        tool_ev = next(e for e in events if e.get("payload", {}).get("tool") == "stack_parse")
        assert tool_ev["parent_span_id"] == root.span_id
        for ev in events:
            if ev.get("schema_version"):
                assert not validate_event(ev)

    def test_abnormal_close_on_dangling_ask(self, work_dir: Path):
        tracer = RepairRunTracer(str(work_dir))
        run_id = tracer.begin("issue")
        TraceSpanContext.push("ask:patcher:patch")
        assert TraceSpanContext.depth() == 2
        n = tracer.close_dangling_ask_spans()
        assert n == 1
        assert TraceSpanContext.depth() == 1
        events = tracer.store.load_trace_events(run_id)
        closed = [e for e in events if e["event"] == "span_closed"]
        assert len(closed) == 1
        assert closed[0]["status"] == STATUS_ERROR
        assert closed[0]["payload"]["reason"] == "abnormal"
        tracer.end_root_span()
        assert TraceSpanContext.depth() == 0


class TestInferStatus:
    def test_cancelled(self):
        assert infer_status("repair_cancelled") == "cancelled"

    def test_finished_failed(self):
        assert infer_status("repair_finished", {"status": "failed"}) == "error"


class TestSampleFile:
    def test_sample_jsonl_valid(self):
        path = Path("docs/examples/canonical-trace-sample.jsonl")
        assert path.is_file()
        events = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(events) >= 5
        ordered = order_events(events)
        assert ordered[0]["event"] == "repair_started"
        assert ordered[-1]["event"] in ("repair_finished", "span_closed")
        for ev in ordered:
            assert not validate_event(ev)
        seqs = [e["seq"] for e in ordered]
        assert seqs == sorted(seqs)
