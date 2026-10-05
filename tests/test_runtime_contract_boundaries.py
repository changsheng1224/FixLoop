"""Regression checks for the breaking L1/L2 and tool contract changes."""

import ast
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agent_runtime.schema_utils import auto_schema, auto_validate
from agent_runtime.tool_context import ToolContext
from agent_runtime.tool_result import ToolResult
from agent_runtime.tool_spec import ToolRegistry, ToolSpec
from agent_runtime.tools import tool_read_file, tool_write_file
from src.repair.execution.edit_lock import EditLockState


def test_l1_has_no_layer2_imports():
    root = Path(__file__).resolve().parents[1] / "agent_runtime"
    violations = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            if any(name == "src" or name.startswith("src.") for name in modules):
                violations.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not violations


def test_dataclass_schema_preserves_nullable_lists_and_defaults():
    @dataclass
    class Args:
        count: int
        label: str | None = None
        values: list[int] = field(default_factory=list)

    schema = auto_schema(Args)
    assert schema["required"] == ["count"]
    assert schema["properties"]["label"]["default"] is None
    assert auto_validate(Args, {"count": 2, "label": None, "values": [1, 2]})["values"] == [1, 2]
    for arguments in ({"count": True}, {"count": "2"}, {"count": 2, "values": ["1"]}):
        with pytest.raises(ValueError, match="invalid_argument_type"):
            auto_validate(Args, arguments)


def test_registry_explicit_zero_and_false_override_defaults():
    registry = ToolRegistry([ToolSpec("read", max_retries=3, requires_approval=True)])
    registry.bind_execution_tools({"read": {"max_retries": 0, "requires_approval": False}})
    assert registry.get("read").max_retries == 0
    assert registry.get("read").requires_approval is False


def test_same_workspace_edit_policies_are_isolated(tmp_path):
    (tmp_path / "a.py").write_text("old = 1\n", encoding="utf-8")
    allowed = EditLockState(repo_root=tmp_path, allowed_edit={"a.py"})
    denied = EditLockState(repo_root=tmp_path, allowed_edit=set())
    first = ToolContext(str(tmp_path), edit_lock=allowed)
    second = ToolContext(str(tmp_path), edit_lock=denied)
    assert tool_read_file(first, {"path": "a.py"}).ok
    result = tool_write_file(second, {"path": "a.py", "content": "new = 2\n"})
    assert isinstance(result, ToolResult) and result.failed
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "old = 1\n"


def test_policy_failure_cannot_allow_a_write(tmp_path):
    class BrokenPolicy:
        write_serial = False

        def check_write(self, path):
            raise RuntimeError("policy unavailable")

    context = ToolContext(str(tmp_path), edit_lock=BrokenPolicy())
    with pytest.raises(RuntimeError, match="policy unavailable"):
        tool_write_file(context, {"path": "a.py", "content": "unsafe"})
    assert not (tmp_path / "a.py").exists()


def test_registry_rejects_compact_schema():
    with pytest.raises(ValueError, match="JSON Schema"):
        ToolRegistry().bind_execution_tools({"read": {"schema": {"path": "str"}}})


def test_missing_schema_fails_at_registration():
    with pytest.raises(ValueError, match="JSON Schema"):
        ToolRegistry([ToolSpec("read", input_schema={"path": "str"})])


def test_executor_rejects_untyped_custom_tool(workspace):
    from agent_runtime.config import AgentConfig
    from agent_runtime.providers.clients import FakeModelClient
    from agent_runtime.runtime import Agent
    from agent_runtime.tool_executor import ToolExecutor

    agent = Agent(AgentConfig(provider="fake", approval="auto"), FakeModelClient([]), workspace)
    agent.tools["list_files"]["run"] = lambda args: "looks successful"
    result = ToolExecutor(agent, approval_policy="auto").execute_gated("list_files", {"path": "."})
    assert result.error_code == "invalid_tool_result"
    assert result.failed and not result.retryable
