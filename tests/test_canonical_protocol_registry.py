from __future__ import annotations


def test_tool_call_protocol_normalizes_xml_text_and_unknown_tool():
    from agent_runtime.canonical_protocol import parse_tool_call

    xml = parse_tool_call(
        '<function_calls><invoke name="read_file"><parameter name="path">a.py</parameter></invoke></function_calls>',
        expected_tools={"read_file"},
    )
    assert xml.response_kind == "tool_call"
    assert xml.payload["call"].name == "read_file"

    text = parse_tool_call('<tool>{"name":"read_file","args":{"path":"a.py"}}</tool>')
    assert text.payload["call"].arguments["path"] == "a.py"
    denied = parse_tool_call('{"name":"run_shell","args":{}}', expected_tools={"read_file"})
    assert denied.status == "stop"


def test_json_recovery_handles_markdown_nested_and_truncation():
    from agent_runtime.json_recovery import repair_structured_output

    parsed = repair_structured_output('answer:\n```json\n{"patches":[{"file_path":"a.py"}]}\n```')
    assert parsed.ok and parsed.value["patches"][0]["file_path"] == "a.py"
    nested = repair_structured_output('{"a":{"b":[1,2,]}}')
    assert nested.ok and nested.value["a"]["b"] == [1, 2]
    truncated = repair_structured_output('{"a": [1, 2')
    assert not truncated.ok and truncated.error_code == "truncated_json"


def test_observation_store_deduplicates_and_keeps_raw_ref(tmp_path):
    from agent_runtime.context_runtime import ObservationStore

    state = {}
    store = ObservationStore(state, str(tmp_path))
    first = store.put("read_file", {"path": "a.py"}, "line 1", summary="line 1")
    second = store.put("read_file", {"path": "a.py"}, "different", summary="line 1")
    assert first.observation_id == second.observation_id
    assert store.expand(first.observation_id) == "line 1"


def test_registry_is_capability_and_policy_source():
    from src.tools.spec import default_repair_tool_registry

    registry = default_repair_tool_registry()
    visible = registry.capabilities_for("patcher", phase="patch")
    names = {item["name"] for item in visible["tools"]}
    assert {"read_file", "apply_patch", "quick_test"}.issubset(names)
    assert "run_shell" in visible["denied"]
    apply_patch = registry.get("apply_patch")
    assert apply_patch.side_effect == "write"
    assert apply_patch.replay_policy == "never_replay"


def test_mcp_specs_and_capability_query_share_registry():
    from agent_runtime.mcp.registry import (
        build_github_mcp_tool_specs,
        build_mock_github_mcp_client,
    )
    from src.middleware import ToolGateway
    from src.tools.spec import ToolRegistry, project_tool_specs

    client, _ = build_mock_github_mcp_client()
    specs = build_github_mcp_tool_specs(client)
    registry = ToolRegistry(specs)
    gateway = ToolGateway(registry=registry)
    gateway.bind_tools(project_tool_specs(specs))
    view = gateway.query_capabilities("patcher", phase="patch")
    names = {tool["name"] for tool in view["tools"]}
    draft = registry.get("github_create_draft_pr")

    assert "github_get_issue" in names
    assert "github_create_draft_pr" in names
    assert draft is not None and draft.side_effect == "external_write"
    assert draft.provider == "mcp"
    assert draft.protocol_schema["additionalProperties"] is False


def test_mcp_errors_have_canonical_retry_semantics():
    from agent_runtime.mcp.errors import McpSchemaError, McpUnavailableError

    invalid = McpSchemaError("bad arguments").metadata()
    unavailable = McpUnavailableError("offline").metadata()

    assert invalid["tool_error_code"] == "invalid_arguments"
    assert invalid["retryable"] is True
    assert unavailable["tool_error_code"] == "mcp_unavailable"
    assert unavailable["retryable"] is True
