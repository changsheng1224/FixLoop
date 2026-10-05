"""Behavioral regressions for canonical results, schemas, and process drainage."""

import os
import sys

import pytest

from agent_runtime.cancellation import CancellationToken
from agent_runtime.repair_runtime import CanonicalToolCall, observation_from_result
from agent_runtime.tool_result import ToolResult, attach_tool_receipt
from agent_runtime.tool_schema import validate_tool_arguments
from agent_runtime.tools import _run_shell


def test_result_fields_are_the_only_source_for_observation_and_receipt():
    result = ToolResult(status="error", retryable=False, changed_files=["a.py"])
    result.status = "rejected"
    result.error_code = "policy_denied"
    attach_tool_receipt(result, "write_file")
    observation = observation_from_result(CanonicalToolCall.create("write_file", {}), result)
    assert not observation.retryable
    assert observation.status == "validation_error"
    assert observation.changed_files == result.receipt["affected_paths"] == ["a.py"]
    assert result.receipt["status"] == "rejected"
    assert not result.receipt["retryable"]
    exported = result.to_metadata()
    exported["affected_paths"].append("wrong.py")
    exported["receipt"]["retryable"] = True
    assert result.changed_files == ["a.py"]
    assert not result.receipt["retryable"]
    assert "retryable" not in result.metadata


@pytest.mark.parametrize("key", ["tool_status", "retryable", "affected_paths", "receipt"])
def test_control_fields_in_metadata_are_rejected(key):
    with pytest.raises(ValueError, match="ToolResult fields"):
        ToolResult(metadata={key: False})
    result = ToolResult()
    result.metadata[key] = False
    with pytest.raises(ValueError, match="ToolResult fields"):
        attach_tool_receipt(result, "read_file")


@pytest.mark.parametrize(
    "prop,value,valid",
    [
        ({"type": ["object", "null"], "properties": {"x": {"type": "string"}}}, {"x": "ok"}, True),
        ({"type": ["object", "null"]}, None, True),
        ({"anyOf": [{"type": "string"}], "minLength": 5}, "a", False),
        ({"oneOf": [{"type": "integer"}, {"type": "boolean"}]}, "bad", False),
        ({"enum": [1]}, True, False),
        (False, "anything", False),
        ({"const": "fixed"}, "other", False),
    ],
)
def test_schema_composition_and_json_types(prop, value, valid):
    _, errors = validate_tool_arguments({"type": "object", "properties": {"v": prop}}, {"v": value})
    assert (not errors) is valid


def test_mcp_validates_full_schema_including_root_constraints_and_local_refs():
    from agent_runtime.mcp.arguments import validate_arguments
    from agent_runtime.mcp.errors import McpSchemaError

    schema = {
        "type": "object",
        "$defs": {"id": {"type": "integer", "minimum": 1}},
        "properties": {"id": {"$ref": "#/$defs/id"}, "label": {"type": "string"}},
        "oneOf": [{"required": ["id"]}, {"required": ["label"]}],
    }
    assert validate_arguments(tool_name="lookup", schema=schema, arguments={"id": 1}) == {"id": 1}
    for args in ({"id": 0}, {"id": 1, "label": "both"}, {}):
        with pytest.raises(McpSchemaError):
            validate_arguments(tool_name="lookup", schema=schema, arguments=args)


def test_schema_keeps_standard_open_object_semantics_and_blocks_remote_references():
    _, errors = validate_tool_arguments(
        {"type": "object", "allOf": [{"properties": {"n": {"type": "integer"}}}]},
        {"n": 1},
    )
    assert not errors
    _, errors = validate_tool_arguments(
        {"type": "object", "$ref": "https://example.invalid/schema"}, {}
    )
    assert errors[0]["code"] == "unresolvable_schema_reference"


def test_mcp_discovery_and_dispatch_keep_the_same_complete_schema():
    from agent_runtime.mcp.client import McpClient
    from agent_runtime.mcp.errors import McpSchemaError
    from agent_runtime.mcp.registry import build_github_mcp_tool_registry

    schema = {
        "type": "object",
        "$defs": {"id": {"type": "integer", "minimum": 1}},
        "properties": {"id": {"$ref": "#/$defs/id"}},
        "required": ["id"],
    }

    class Transport:
        calls = 0

        def request(self, method, params=None):
            if method == "tools/list":
                return {"tools": [{"name": "github_get_issue", "inputSchema": schema}]}
            self.calls += 1
            return {"content": [{"type": "text", "text": "ok"}]}

    transport = Transport()
    client = McpClient(transport)
    tools = build_github_mcp_tool_registry(client)
    assert tools["github_get_issue"]["schema"]["$defs"] == schema["$defs"]
    assert client.call_tool("github_get_issue", {"id": 1}).content == "ok"
    with pytest.raises(McpSchemaError):
        client.call_tool("github_get_issue", {"id": 0})
    assert transport.calls == 1


def test_post_execution_rejection_seals_matching_ledger_and_observation(workspace, monkeypatch):
    from agent_runtime.agent_loop import AgentLoop
    from agent_runtime.loop_policy import LoopPolicy
    from agent_runtime.model_turn import ToolCall
    from tests.test_native_tool_batch import make_agent

    def reject(self, context, name, args, result):
        result.status = "rejected"
        result.error_code = "postcondition_failed"
        result.retryable = False
        return True

    monkeypatch.setattr(LoopPolicy, "review_result", reject)
    agent, _ = make_agent(workspace, [ToolCall("read_file", {"path": "README.md"}, "read")])
    AgentLoop(agent).run("read", skip_plan=True)
    action = agent.session["action_ledger"][-1]
    observation = agent.session["tool_observations"][-1]
    assert action["status"] == "failed"
    assert action["receipt"]["status"] == "rejected"
    assert action["receipt"]["call_id"] == "read"
    assert not action["receipt"]["retryable"]
    assert observation["status"] == "validation_error"
    assert observation["receipt"] == action["receipt"]


@pytest.mark.parametrize("with_token", [False, True])
def test_shell_drains_both_pipes_while_bounding_retained_output(tmp_path, monkeypatch, with_token):
    monkeypatch.setenv("FIXLOOP_SHELL_MAX_BYTES", "4096")
    result = _run_shell(
        [sys.executable, "-c", "import os; os.write(1,b'x'*400000); os.write(2,b'y'*400000)"],
        "large stdout and stderr",
        tmp_path,
        os.environ.copy(),
        5,
        CancellationToken() if with_token else None,
    )
    assert result.ok, result.content
    assert result.data["exit_code"] == 0
    assert result.output_truncated
    assert "stdout:" in result.content and "stderr:" in result.content
    assert len(result.content) < 10000
    assert result.metadata["termination_guaranteed"]


def test_shell_timeout_retains_output_and_confirms_cleanup(tmp_path):
    result = _run_shell(
        [sys.executable, "-c", "import time; print('started',flush=True); time.sleep(30)"],
        "timeout",
        tmp_path,
        os.environ.copy(),
        1,
    )
    assert result.error_code == "tool_timeout"
    assert "started" in result.content
    assert result.metadata["termination_guaranteed"]


def test_shell_pre_cancel_does_not_start_process(tmp_path):
    token = CancellationToken()
    token.cancel()
    result = _run_shell(["nonexistent-command"], "cancelled", tmp_path, os.environ.copy(), 5, token)
    assert result.status == "cancelled"
    assert not result.retryable


def test_shell_output_is_utf8_independent_of_windows_locale(tmp_path):
    result = _run_shell(
        [sys.executable, "-c", "import os; os.write(1,bytes.fromhex('e4b8ade69687'))"],
        "utf8",
        tmp_path,
        os.environ.copy(),
        5,
    )
    assert result.ok
    assert "中文" in result.content


def test_profile_launch_failure_is_an_environment_result(tmp_path, monkeypatch):
    from src.repair.verification.verification_profiles import VerificationProfile, VerificationStep
    from src.repair.verification.verification_runner import run_profile

    resolved = str(tmp_path / "bin" / "runner")
    monkeypatch.setattr("shutil.which", lambda _: resolved)

    def fail_launch(command, **kwargs):
        assert command[0] == resolved
        raise FileNotFoundError("executable disappeared after discovery")

    monkeypatch.setattr("subprocess.run", fail_launch)
    profile = VerificationProfile(
        "example",
        "example",
        (),
        test_steps=(VerificationStep("test", ("runner", "test"), "target_tests"),),
    )
    result = run_profile(tmp_path, profile)
    assert not result["all_passed"]
    assert result["category"] == "verification_environment_failed"
    assert "disappeared" in result["error"]


def test_compatibility_shims_stay_removed():
    """The 2026-10-05 compat removal must not be silently reintroduced."""
    import agent_runtime.features.memory.core as memory_core
    import agent_runtime.stop_reasons as stop_reasons

    assert not hasattr(stop_reasons, "normalize_stop_reason")
    assert not hasattr(stop_reasons, "stop_reason_detail_from_legacy")
    assert not hasattr(stop_reasons, "RESERVED_STOP_REASONS")
    assert not hasattr(memory_core, "normalize_memory_state")
    assert not hasattr(memory_core, "MAX_FILE_SUMMARIES")

    from agent_runtime.context_runtime import ContextPolicyEngine
    from agent_runtime.features.memory.durable import UserProfileStore
    from agent_runtime.task_state import TaskState

    assert not hasattr(TaskState, "stop")
    assert not hasattr(UserProfileStore, "remove")
    assert hasattr(ContextPolicyEngine, "select_with_result")
    assert not hasattr(ContextPolicyEngine, "select")

    from src.benchmark.swebench import harness
    from src.repair.execution import patch_applier
    from src.repair.phase_clock import PhaseTimeoutConfig

    assert not hasattr(PhaseTimeoutConfig, "from_repair_timeout")
    assert hasattr(PhaseTimeoutConfig, "with_repair_total_cap")
    assert not hasattr(patch_applier, "extract_json_block")
    assert not hasattr(harness, "_parse_resolved")


def test_intent_payload_and_memory_shims_stay_removed():
    """The 2026-10-05 (cont.) intent/payload/memory removal must not creep back."""
    import inspect

    import agent_runtime.intent.llm_fallback as llm_fallback

    assert hasattr(llm_fallback, "maybe_refine")
    assert not hasattr(llm_fallback, "maybe_refine_graph")

    import agent_runtime.tools as tools

    assert not hasattr(tools, "json")

    from agent_runtime.canonical_trace import validate_event

    assert "require_canonical" not in inspect.signature(validate_event).parameters

    from agent_runtime.features.memory.durable import DurableMemoryStore

    assert list(inspect.signature(DurableMemoryStore._read_topic).parameters)[1:] == [
        "topic",
        "strategy",
    ]
