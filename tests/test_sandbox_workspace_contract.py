"""Workspace wire boundary rejects malformed data before dispatch."""

from dataclasses import replace

import pytest

from agent_runtime.linux_sandbox.models import SandboxRequest
from agent_runtime.linux_sandbox.workspace_contract import (
    MAX_REQUEST_BYTES,
    MAX_RESULT_BYTES,
    relative_path,
    tool_result_from_wire,
    tool_result_to_wire,
)
from agent_runtime.tool_result import ToolResult


def workspace_request(**changes):
    request = SandboxRequest(
        "ws",
        "task",
        "run",
        "call",
        "workspace",
        (),
        schema_version="2",
        tool_name="read_file",
        tool_args={"path": "src/a.py"},
    )
    return replace(request, **changes)


def test_workspace_roundtrip_preserves_arguments_and_versions():
    request = workspace_request(
        tool_name="patch_file",
        tool_args={"path": "src/a.py", "old_text": "a", "new_text": "b"},
        base_versions={"src/a.py": "a" * 64},
    )
    assert SandboxRequest.from_wire(request.to_wire()) == request


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": []},
        {"operation": []},
        {"tool_name": "code_lookup"},
        {"schema_version": "1"},
        {"argv": ["/bin/sh"]},
        {"cwd_relative": "src"},
        {"workspace_id": 1},
        {"timeout_s": float("nan")},
        {"timeout_s": True},
        {"output_limit_bytes": True},
        {"generation": -1},
        {"owner_token": []},
        {"tool_args": {"path": "a", "value": float("inf")}},
        {"tool_args": {"path": "a", "value": b"bytes"}},
        {"base_versions": {"a": "not-a-digest"}},
        {"base_versions": {"a": False}},
        {"base_versions": {"src\\a.py": "a" * 64}},
        {"base_versions": {"a": "a" * 64}},
    ],
)
def test_workspace_rejects_malformed_or_mixed_request(changes):
    with pytest.raises(ValueError):
        SandboxRequest.from_wire({**workspace_request().to_wire(), **changes})


def test_request_limit_is_checked_before_dispatch():
    request = workspace_request(tool_args={"path": "a", "content": "x" * MAX_REQUEST_BYTES})
    with pytest.raises(ValueError, match="too large"):
        SandboxRequest.from_wire(request.to_wire())


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/etc/passwd",
        "C:/repo/a",
        "a//b",
        "a/./b",
        "a:stream",
        ".git/config",
        ".AGENT/state",
        ".env/a",
        ".aws/a",
        ".ssh/a",
        "a/.env.local/file",
        ".fixloop-s0-private-token",
        ".fixloop-private-token",
        "aux.py",
        "COM1/file",
        "a?b",
        "a\nb",
        "a.",
        "a ",
        "a/../b",
    ],
)
def test_lexical_paths_reject_escapes_aliases_and_control_files(path):
    with pytest.raises(ValueError):
        relative_path(path)


def test_unicode_relative_paths_and_optional_root():
    assert relative_path("源码\\文件 a.py") == "源码/文件 a.py"
    assert relative_path(".", allow_root=True) == "."
    with pytest.raises(ValueError):
        relative_path(".")


def test_tool_result_roundtrip_keeps_retrieval_evidence_and_typed_status():
    result = ToolResult(
        "Error: this is ordinary source text",
        data={"symbols": ["f"]},
        metadata={"source_dependencies": {"a.py": "a" * 64}},
    )
    decoded = tool_result_from_wire(tool_result_to_wire(result))
    assert decoded == result
    assert decoded.ok


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "invented"},
        {"retryable": None},
        {"duration_ms": True},
        {"changed_files": [1]},
        {"receipt": []},
        {"metadata": {"tool_status": "success"}},
        {"data": float("nan")},
        {"data": b"bytes"},
        {"extra": "field"},
        {"content": "x" * MAX_RESULT_BYTES},
    ],
)
def test_tool_result_rejects_invalid_or_unbounded_wire(changes):
    with pytest.raises(ValueError):
        tool_result_from_wire({**tool_result_to_wire(ToolResult()), **changes})


def test_oversized_result_becomes_explicit_error():
    result = tool_result_from_wire(tool_result_to_wire(ToolResult("x" * MAX_RESULT_BYTES)))
    assert not result.ok
    assert result.error_code == "output_too_large"


@pytest.mark.parametrize(
    "changes", [{"tool_args": None}, {"base_versions": None}, {"tool_name": None}]
)
def test_command_request_cannot_smuggle_workspace_fields(changes):
    wire = SandboxRequest("ws", "task", "run", "call", "command", ("python",)).to_wire()
    with pytest.raises(ValueError):
        SandboxRequest.from_wire({**wire, **changes})
