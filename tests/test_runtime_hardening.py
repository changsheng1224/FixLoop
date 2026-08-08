"""Focused tests for Agent Runtime contracts and failure boundaries."""

import json

import pytest

from agent_runtime.budget_manager import BudgetManager
from agent_runtime.canonical_trace import validate_runtime_trace
from agent_runtime.context_runtime import validate_context_manifest
from agent_runtime.metrics import MetricsRegistry
from agent_runtime.providers.contracts import (
    ProviderErrorCode,
    normalize_provider_error,
)
from agent_runtime.providers.retry_policy import RetryPolicy
from agent_runtime.runtime_contracts import RuntimePhase, RuntimeStateMachine
from agent_runtime.session_store import SessionStore
from agent_runtime.tool_result import ToolResult, ToolStatus, build_tool_receipt


def test_runtime_state_machine_requires_reason_for_terminal_transition():
    machine = RuntimeStateMachine()
    machine.transition(RuntimePhase.REASONING)
    machine.transition(RuntimePhase.FINALIZING)
    with pytest.raises(ValueError, match="stop_reason"):
        machine.transition(RuntimePhase.COMPLETED)

    machine.transition(RuntimePhase.COMPLETED, stop_reason="final")
    with pytest.raises(ValueError, match="terminal runtime"):
        machine.transition(RuntimePhase.REASONING)


def test_provider_error_and_retry_policy_are_deterministic():
    error = normalize_provider_error(TimeoutError("model timed out"), provider="test")
    assert error.code == ProviderErrorCode.TIMEOUT
    assert error.retryable is True
    assert RetryPolicy(max_attempts=2, idempotent=False).should_retry(
        attempt=1, retryable=True, deadline_s=10
    ) is False
    assert RetryPolicy(max_attempts=2, base_delay_s=0).delay(1) == 0


def test_budget_multi_resource_reservation_is_atomic():
    manager = BudgetManager({"llm_calls": 1, "writes": 1})
    rejected = manager.reserve_many({"llm_calls": 1, "writes": 2})
    assert any(not item.allowed for item in rejected)
    assert manager.snapshot()["used"] == {"llm_calls": 0.0, "writes": 0.0}
    accepted = manager.reserve_many({"llm_calls": 1, "writes": 1})
    assert all(item.allowed for item in accepted)
    assert {item["resource"] for item in manager.backpressure()} == {"llm_calls", "writes"}


def test_session_store_revision_and_atomic_persistence(tmp_path):
    first = SessionStore(str(tmp_path))
    second = SessionStore(str(tmp_path))
    session = {"id": "shared", "messages": []}
    saved = first.save(session)
    assert saved["revision"] == 1
    with pytest.raises(RuntimeError, match="revision mismatch"):
        second.save({"id": "shared", "messages": ["stale"]}, expected_revision=0)
    loaded = second.load("shared")
    assert loaded["revision"] == 1
    assert (tmp_path / ".agent" / "sessions" / ".shared.lock").is_file()


def test_receipt_and_runtime_trace_semantics():
    result = ToolResult(
        content="changed",
        status=ToolStatus.SUCCESS.value,
        changed_files=["src/app.py"],
        duration_ms=12,
    )
    receipt = build_tool_receipt("patch_file", result, args_hash="args-1", run_id="run-1")
    assert receipt["schema_version"] == "1"
    assert receipt["receipt_id"].startswith("receipt-")

    events = [
        {
            "schema_version": "1",
            "run_id": "run-1",
            "trace_id": "run-1",
            "span_id": "s1",
            "parent_span_id": None,
            "event_type": "run_started",
            "event": "run_started",
            "timestamp": "2026-01-01T00:00:00Z",
            "created_at": "2026-01-01T00:00:00Z",
            "status": "ok",
            "seq": 1,
        },
        {
            "schema_version": "1",
            "run_id": "run-1",
            "trace_id": "run-1",
            "span_id": "s2",
            "parent_span_id": "s1",
            "event_type": "tool_executed",
            "event": "tool_executed",
            "timestamp": "2026-01-01T00:00:01Z",
            "created_at": "2026-01-01T00:00:01Z",
            "status": "ok",
            "seq": 2,
            "payload": {"tool": "patch_file", "receipt": receipt},
        },
        {
            "schema_version": "1",
            "run_id": "run-1",
            "trace_id": "run-1",
            "span_id": "s3",
            "parent_span_id": None,
            "event_type": "run_finished",
            "event": "run_finished",
            "timestamp": "2026-01-01T00:00:02Z",
            "created_at": "2026-01-01T00:00:02Z",
            "status": "ok",
            "seq": 3,
        },
    ]
    assert validate_runtime_trace(events) == []
    malformed = json.loads(json.dumps(events))
    malformed[1]["payload"]["receipt"] = {"bad": True}
    assert any("invalid_tool_receipt" in issue for issue in validate_runtime_trace(malformed))


def test_context_manifest_and_histogram_contracts():
    assert validate_context_manifest({"schema_version": "x"})
    registry = MetricsRegistry()
    registry.histogram_observe("fixloop_tool_duration_ms", 120)
    rendered = registry.render()
    assert "fixloop_tool_duration_ms_bucket" in rendered
    assert "fixloop_tool_duration_ms_count" in rendered
