from __future__ import annotations


def test_canonical_tool_call_is_stable():
    from agent_runtime.repair_runtime import CanonicalToolCall

    first = CanonicalToolCall.create("read_file", {"path": "a.py", "start": 1})
    second = CanonicalToolCall.create("read_file", {"start": 1, "path": "a.py"})
    assert first.call_id == second.call_id
    assert first.arguments == {"path": "a.py", "start": 1}


def test_schema_is_shared_by_provider_and_validator():
    from agent_runtime.loop_protocols import build_native_tools
    from agent_runtime.tool_schema import validate_tool_arguments

    registry = {
        "read_file": {
            "schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer", "default": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            "description": "read",
        }
    }
    provider = build_native_tools(registry)[0]["input_schema"]
    assert provider["required"] == ["path"]
    assert provider["additionalProperties"] is False
    normalized, errors = validate_tool_arguments(
        registry["read_file"]["schema"], {"path": "x", "start": "2"}
    )
    assert errors[0]["code"] == "invalid_argument_type"
    assert normalized["start"] == "2"


def test_schema_reports_unknown_and_missing_arguments():
    from agent_runtime.tool_schema import validate_tool_arguments

    _, errors = validate_tool_arguments(
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        {"extra": 1},
    )
    assert {error["code"] for error in errors} == {
        "missing_required_argument",
        "unknown_argument",
    }


def test_deadline_and_group_budget_are_independent(monkeypatch):
    from agent_runtime.repair_runtime import ExecutionDeadline, RepairBudget

    budget = RepairBudget(max_tool_calls=3, max_write_calls=1, max_verify_calls=1)
    assert budget.allow_tool("write")
    budget.record_tool("write")
    assert not budget.allow_tool("write")
    assert budget.allow_tool("verify")

    monkeypatch.setattr("agent_runtime.repair_runtime.time.monotonic", lambda: 100.0)
    deadline = ExecutionDeadline(0.001)
    monkeypatch.setattr("agent_runtime.repair_runtime.time.monotonic", lambda: 100.005)
    assert deadline.expired()


def test_observation_normalizes_validation_error():
    from agent_runtime.repair_runtime import CanonicalToolCall, observation_from_result
    from agent_runtime.tool_result import ToolResult

    result = ToolResult(
        "Error",
        status="rejected",
        error_code="invalid_args",
    )
    observation = observation_from_result(CanonicalToolCall.create("read_file", {}), result)
    assert observation.status == "validation_error"
    assert observation.retryable is True
