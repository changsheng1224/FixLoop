"""Focused coverage for the provider-neutral tool runtime contracts."""

from __future__ import annotations

import threading
import time

import pytest

from agent_runtime.context_runtime import (
    action_recovery_decision,
    build_action_record,
    transition_action,
)
from agent_runtime.tool_dag import ToolDAGExecutor, ToolNode
from agent_runtime.tool_resilience import ToolResilienceController
from agent_runtime.tool_result import ToolErrorCode, ToolResult, ToolStatus, normalize_tool_result
from agent_runtime.tool_schema import schema_to_json, validate_tool_arguments
from agent_runtime.tool_timeout import ToolTimeoutError, run_with_timeout
from src.tools.spec import ToolSpec, project_tool_specs


def _sleep_forever() -> None:
    time.sleep(10)


def test_legacy_error_is_normalized_to_typed_result():
    result = normalize_tool_result("Error: provider unavailable", tool_name="lookup")
    assert isinstance(result, ToolResult)
    assert result.status == ToolStatus.ERROR.value
    assert result.error_code == ToolErrorCode.TOOL_EXECUTION_FAILED.value
    assert result.retryable is True


def test_full_json_schema_validation_and_projection():
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["fast", "safe"]},
            "count": {"type": "integer", "minimum": 1},
            "options": {
                "type": "object",
                "properties": {"dry": {"type": "boolean"}},
                "required": ["dry"],
                "additionalProperties": False,
            },
        },
        "required": ["mode", "count", "options"],
        "additionalProperties": False,
    }
    normalized, errors = validate_tool_arguments(schema, {"mode": "bad", "count": 0, "options": {}})
    assert normalized["count"] == 0
    assert {item["code"] for item in errors} >= {"enum_violation", "minimum_violation", "missing_required_argument"}
    assert schema_to_json(schema)["additionalProperties"] is False
    projected = project_tool_specs([ToolSpec(name="inspect", protocol_schema=schema)])["inspect"]
    assert projected["json_schema"]["properties"]["mode"]["enum"] == ["fast", "safe"]


def test_action_state_machine_blocks_invalid_transition_and_recovers_uncertain():
    action = build_action_record("write_file", {"path": "a.txt"}, revision=2, status="dispatched")
    action = transition_action(action.__dict__, "uncertain", reason="tool_timeout")
    assert action["status"] == "uncertain"
    assert action_recovery_decision({"action_ledger": [action]}, "write_file", {"path": "a.txt"})["decision"] == "revalidate"
    with pytest.raises(ValueError):
        transition_action(action, "planned")


def test_process_timeout_guarantees_termination():
    with pytest.raises(ToolTimeoutError) as exc_info:
        run_with_timeout(_sleep_forever, timeout_s=1, mode="process")
    assert exc_info.value.termination_guaranteed is True


def test_rate_limit_and_circuit_breaker_decisions():
    controller = ToolResilienceController()
    spec = {"rate_limit_per_minute": 1, "circuit_breaker_threshold": 2}
    assert controller.before("remote", spec).allowed
    assert controller.before("remote", spec).reason == "rate_limited"
    controller.after("remote", spec, success=False)
    controller.after("remote", spec, success=False)
    assert controller.before("remote", {"circuit_breaker_threshold": 2}).reason == "circuit_open"


def test_tool_dag_parallel_reads_and_serializes_writes():
    active = 0
    max_active = 0
    lock = threading.Lock()

    def execute(name, args):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return f"{name}:ok"

    result = ToolDAGExecutor(execute, max_workers=2).run(
        [
            ToolNode("read-a", "read_file"),
            ToolNode("read-b", "read_file"),
            ToolNode("write", "write_file", depends_on=("read-a",), side_effect="write"),
        ]
    )
    assert all(item.status == "success" for item in result.values())
    assert max_active == 2
