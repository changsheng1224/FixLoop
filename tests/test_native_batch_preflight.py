"""Native batch admission: pure schema checks and lossless protocol pairing."""

from copy import deepcopy

import pytest

from agent_runtime.agent_loop import AgentLoop
from agent_runtime.model_turn import (
    FinishKind,
    ModelTurnRequest,
    ModelTurnResult,
    ProviderFinish,
    ToolCall,
)
from agent_runtime.providers.clients import AnthropicCompatibleModelClient, FakeNativeToolClient
from agent_runtime.tool_batch import ToolBatchProtocolError, ToolCallBatch
from agent_runtime.tool_context import ToolContext
from tests.test_native_tool_batch import make_agent


class TurnClient(FakeNativeToolClient):
    def __init__(self, turn):
        super().__init__(["<final>done</final>"])
        self.turn = turn
        self.requests = []

    def complete_turn(self, request):
        self.requests.append(request)
        return self.turn if len(self.requests) == 1 else super().complete_turn(request)


def raw_blocks(calls):
    return [
        {"type": "tool_use", "id": c.call_id, "name": c.name, "input": deepcopy(c.arguments)}
        for c in calls
    ]


def test_missing_argument_is_paired_without_dispatch_or_budget_reservation(workspace):
    calls = [
        ToolCall("read_file", {}, "missing"),
        ToolCall("read_file", {"path": "README.md"}, "good"),
    ]
    agent, client = make_agent(workspace, calls)
    loop = AgentLoop(agent)
    assert "done" in loop.run("inspect", skip_plan=True)
    observations = agent.session["tool_observations"]
    assert [o["call_id"] for o in observations] == ["missing", "good"]
    assert observations[0]["failure_class"] == "invalid_arguments"
    assert "path" in client.requests[1].messages[-1]["content"][0]["content"]
    assert agent.quota.quota_summary()["total"]["used"] == 1
    assert loop._repair_budget.tool_calls == 1
    assert len(agent.session["action_ledger"]) == 1
    assert ":good:" in agent.session["action_ledger"][0]["idempotency_key"]


@pytest.mark.parametrize(
    "damage", ["id", "name", "arguments", "missing", "duplicate", "order", "block", "root"]
)
def test_raw_protocol_mismatch_rejects_batch_before_any_real_effect(
    workspace, temp_workspace, damage
):
    calls = [
        ToolCall("read_file", {"path": "README.md"}, "read"),
        ToolCall("write_file", {"path": "README.md", "content": "changed"}, "write"),
    ]
    content = raw_blocks(calls)
    if damage == "id":
        content[1]["id"] = "other"
    elif damage == "name":
        content[1]["name"] = "read_file"
    elif damage == "arguments":
        content[1]["input"]["content"] = "different"
    elif damage == "missing":
        content.pop()
    elif damage == "duplicate":
        content.append(deepcopy(content[0]))
    elif damage == "order":
        content.reverse()
    elif damage == "block":
        content.append("not a content block")
    else:
        content = {"type": "tool_use"}
    agent, _ = make_agent(workspace, calls)
    agent.model_client = TurnClient(
        ModelTurnResult(
            tool_calls=calls, content=content, finish=ProviderFinish(FinishKind.TOOL_CALLS)
        )
    )
    before = (temp_workspace / "README.md").read_text()
    with pytest.raises(ToolBatchProtocolError):
        AgentLoop(agent).run("inspect and edit", skip_plan=True)
    assert (temp_workspace / "README.md").read_text() == before
    assert not agent.session.get("tool_observations")
    assert not agent.session.get("action_ledger")
    assert agent.quota.quota_summary()["total"]["used"] == 0


@pytest.mark.parametrize(
    "raw_input", [None, [], "{}", False], ids=["null", "array", "string", "boolean"]
)
def test_provider_preserves_malformed_input_until_protocol_rejection(
    workspace, monkeypatch, raw_input
):
    client = AnthropicCompatibleModelClient("fixture", "https://example.invalid", "fixture")
    data = {
        "content": [{"type": "tool_use", "id": "bad", "name": "list_files", "input": raw_input}],
        "stop_reason": "tool_use",
    }
    monkeypatch.setattr(client, "_post_messages", lambda *args, **kwargs: (data, None))
    turn = client.complete_turn(ModelTurnRequest("", [{"role": "user", "content": "inspect"}]))
    assert turn.tool_calls[0].arguments == raw_input
    agent, _ = make_agent(workspace, [])
    agent.model_client = TurnClient(turn)
    with pytest.raises(ToolBatchProtocolError):
        AgentLoop(agent).run("inspect", skip_plan=True)
    assert not agent.session.get("tool_observations")
    assert agent.quota.quota_summary()["total"]["used"] == 0


def test_truncated_turn_discards_even_complete_write_candidates(workspace, temp_workspace):
    calls = [
        ToolCall(
            "write_file", {"path": "README.md", "content": "unsafe"}, "truncated-write-candidate"
        )
    ]
    agent, _ = make_agent(workspace, [])
    client = TurnClient(
        ModelTurnResult(
            tool_calls=calls,
            content=raw_blocks(calls),
            finish=ProviderFinish(FinishKind.MAX_OUTPUT_TOKENS, "max_tokens"),
        )
    )
    agent.model_client = client
    before = (temp_workspace / "README.md").read_text()
    AgentLoop(agent).run("inspect", skip_plan=True)
    assert len(client.requests) == 2
    assert (temp_workspace / "README.md").read_text() == before
    assert not agent.session.get("tool_observations")
    assert not agent.session.get("action_ledger")
    assert "truncated-write-candidate" not in str(client.requests[1].messages)


def test_preflight_freezes_nested_schema_and_preserves_original_arguments(tmp_path):
    schema = {
        "type": "object",
        "properties": {
            "options": {
                "type": "object",
                "properties": {"mode": {"enum": ["safe"]}},
            }
        },
    }
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
            }
        },
        "list_files": {"schema": schema},
    }
    original = {"path": "value.py", "start": "2"}
    calls = [
        ToolCall("read_file", original, "read"),
        ToolCall("list_files", {"options": {"mode": "bad"}}, "invalid"),
    ]
    batch = ToolCallBatch.create(
        calls, run_id="run", turn_id="turn", context=ToolContext(str(tmp_path)), registry=registry
    )
    assert batch.calls[0].argument_errors[0]["code"] == "invalid_argument_type"
    assert batch.calls[0].arguments == original and original["start"] == "2"
    assert batch.calls[1].argument_errors[0]["code"] == "enum_violation"
    schema["properties"]["options"]["properties"]["mode"]["enum"].append("bad")
    frozen = batch.calls[1].context.registry["list_files"]["schema"]
    assert frozen["properties"]["options"]["properties"]["mode"]["enum"] == ["safe"]
    assert all(not call.context.budget_reserved and not call.result for call in batch.calls)


def test_bad_write_is_rejected_but_valid_write_is_not_rolled_back(workspace, temp_workspace):
    calls = [
        ToolCall("read_file", {"path": "README.md"}, "read"),
        ToolCall("write_file", {"path": "README.md", "content": "valid change\n"}, "write"),
        ToolCall(
            "write_file",
            {"path": "README.md", "content": "invalid", "append": "not-bool"},
            "invalid",
        ),
        ToolCall("read_file", {"path": "README.md"}, "after"),
    ]
    agent, client = make_agent(workspace, calls)
    loop = AgentLoop(agent)
    assert "done" in loop.run("inspect and edit", skip_plan=True)
    assert (temp_workspace / "README.md").read_text() == "valid change\n"
    assert loop._repair_budget.tool_calls == 3 and loop._repair_budget.writes == 1
    assert agent.quota.quota_summary()["total"]["used"] == 3
    assert len(agent.session["action_ledger"]) == 3
    assert agent.session["tool_observations"][2]["failure_class"] == "invalid_arguments"
    assert [b["tool_use_id"] for b in client.requests[1].messages[-1]["content"]] == [
        "read",
        "write",
        "invalid",
        "after",
    ]


def test_matching_raw_blocks_allow_text_and_thinking_without_changing_pairing(workspace):
    calls = [ToolCall("read_file", {"path": "README.md"}, "read")]
    content = [
        {"type": "thinking", "thinking": "inspect"},
        {"type": "text", "text": "Reading the file."},
        *raw_blocks(calls),
    ]
    agent, _ = make_agent(workspace, calls)
    client = TurnClient(
        ModelTurnResult(
            tool_calls=calls, content=content, finish=ProviderFinish(FinishKind.TOOL_CALLS)
        )
    )
    agent.model_client = client
    assert "done" in AgentLoop(agent).run("inspect", skip_plan=True)
    assert agent.quota.quota_summary()["total"]["used"] == 1
    assert client.requests[1].messages[-1]["content"][0]["tool_use_id"] == "read"


def test_provider_empty_object_is_valid_for_optional_arguments(workspace, monkeypatch):
    client = AnthropicCompatibleModelClient("fixture", "https://example.invalid", "fixture")
    data = {
        "content": [{"type": "tool_use", "id": "list", "name": "list_files", "input": {}}],
        "stop_reason": "tool_use",
    }
    monkeypatch.setattr(client, "_post_messages", lambda *args, **kwargs: (data, None))
    turn = client.complete_turn(ModelTurnRequest("", [{"role": "user", "content": "inspect"}]))
    agent, _ = make_agent(workspace, [])
    agent.model_client = TurnClient(turn)
    assert "done" in AgentLoop(agent).run("inspect", skip_plan=True)
    assert agent.quota.quota_summary()["total"]["used"] == 1
    assert agent.session["tool_observations"][0]["status"] == "success"


@pytest.mark.parametrize(
    "extra", [{"start": "not-an-int"}, {"unexpected": 1}], ids=["type", "unknown-field"]
)
def test_schema_type_and_unknown_field_rejections_keep_valid_sibling(workspace, extra):
    calls = [
        ToolCall("read_file", {"path": "README.md", **extra}, "invalid"),
        ToolCall("read_file", {"path": "README.md"}, "valid"),
    ]
    agent, client = make_agent(workspace, calls)
    loop = AgentLoop(agent)
    assert "done" in loop.run("inspect", skip_plan=True)
    assert loop._repair_budget.tool_calls == agent.quota.quota_summary()["total"]["used"] == 1
    assert [o["status"] for o in agent.session["tool_observations"]] == [
        "validation_error",
        "success",
    ]
    assert [b["tool_use_id"] for b in client.requests[1].messages[-1]["content"]] == [
        "invalid",
        "valid",
    ]
