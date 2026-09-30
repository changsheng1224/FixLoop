"""P2 routing contracts; no host project process is launched."""

import pytest

from agent_runtime.linux_sandbox.models import SandboxResult
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import build_tool_registry, tool_quick_test, tool_run_shell
from src.middleware import build_repair_gateway
from src.tools.composite import build_repair_canonical_tools


class RecordingBackend:
    def __init__(self, result=None):
        self.requests = []
        self.result = result or SandboxResult(
            "completed",
            exit_code=0,
            stdout_excerpt="1 passed",
            cleanup="confirmed",
            receipt_id="receipt-1",
            policy_digest="digest",
            actual_backend="linux_sandbox",
        )

    def execute(self, request):
        self.requests.append(request)
        return self.result


def context(tmp_path, result=None):
    backend = RecordingBackend(result)
    return ToolContext(root=str(tmp_path), sandbox_backend=backend), backend


def test_quick_test_routes_validated_nodeid_and_fixed_options(tmp_path):
    (tmp_path / "test_app.py").write_text("def test_ok(): assert True\n")
    ctx, backend = context(tmp_path)
    result = tool_quick_test(ctx, {"nodeid": "test_app.py::test_ok"})
    assert result.ok
    assert backend.requests[0].operation == "pytest"
    assert backend.requests[0].argv == (
        "/toolchain/bin/python",
        "-I",
        "-m",
        "pytest",
        "test_app.py::test_ok",
        "-q",
        "--tb=line",
        "--maxfail=3",
    )
    assert result.metadata["sandbox_receipt_id"] == "receipt-1"


def test_quick_test_rejects_escape_and_option(tmp_path):
    ctx, backend = context(tmp_path)
    for target in ("../test_other.py", "-c", "test_app.py::-x"):
        assert tool_quick_test(ctx, {"nodeid": target}).status == "rejected"
    assert backend.requests == []


def test_shell_restricts_executable_after_legacy_allowlist(tmp_path):
    ctx, backend = context(tmp_path)
    assert tool_run_shell(ctx, {"command": "git status"}).status == "rejected"
    assert tool_run_shell(ctx, {"command": "python -c 'print(1)'"}).ok
    assert backend.requests[0].argv[0] == "/toolchain/bin/python"


def test_unverified_backend_cannot_report_success(tmp_path):
    ctx, _ = context(
        tmp_path,
        SandboxResult(
            "completed",
            exit_code=0,
            cleanup="confirmed",
            receipt_id="receipt",
            actual_backend="none",
        ),
    )
    result = tool_run_shell(ctx, {"command": "python -c 'print(1)'"})
    assert result.status == "uncertain"
    assert result.metadata["execution_tier"] == "none"
    assert ctx.sandbox_uncertain


def test_sandbox_grep_never_resolves_host_rg(tmp_path, monkeypatch):
    import agent_runtime.code_exploration.io as retrieval

    (tmp_path / "sample.py").write_text("needle = 1\n")
    ctx, _ = context(tmp_path)
    monkeypatch.setattr(retrieval.shutil, "which", lambda *_: pytest.fail("host rg lookup"))
    from agent_runtime.tools import tool_grep

    assert "needle" in tool_grep(ctx, {"pattern": "needle"})


def test_uncertain_blocks_followup_command_and_write_at_executor(tmp_path):
    from agent_runtime.config import AgentConfig
    from agent_runtime.providers.clients import FakeModelClient
    from agent_runtime.runtime import Agent
    from agent_runtime.tool_executor import ToolExecutor
    from agent_runtime.workspace import WorkspaceContext

    ctx, backend = context(tmp_path, SandboxResult("uncertain", receipt_id="receipt-2"))
    tools = build_tool_registry(ctx)
    agent = Agent(
        config=AgentConfig(provider="fake", approval="auto"),
        model_client=FakeModelClient(["<final>ok</final>"]),
        workspace=WorkspaceContext.build(str(tmp_path)),
        tools=tools,
        cwd=str(tmp_path),
        tool_context=ctx,
    )
    executor = ToolExecutor(agent, approval_policy="auto")
    first = executor.execute("quick_test", {"nodeid": "missing.py"})
    assert first.status == "rejected"
    (tmp_path / "test_a.py").write_text("def test_a(): pass\n")
    second = executor.execute("quick_test", {"nodeid": "test_a.py"})
    assert second.status == "uncertain"
    assert second.metadata["execution_tier"] == "none"
    assert "rollback_attempted" not in second.metadata
    blocked = executor.execute("write_file", {"path": "new.py", "content": "x=1"})
    assert blocked.error_code == "execution_uncertain"
    assert not (tmp_path / "new.py").exists()
    assert len(backend.requests) == 1


def test_executor_preserves_actual_tier_and_sandbox_receipt(tmp_path):
    from agent_runtime.config import AgentConfig
    from agent_runtime.providers.clients import FakeModelClient
    from agent_runtime.runtime import Agent
    from agent_runtime.tool_executor import ToolExecutor
    from agent_runtime.workspace import WorkspaceContext

    (tmp_path / "test_a.py").write_text("def test_a(): pass\n")
    ctx, backend = context(tmp_path)
    agent = Agent(
        config=AgentConfig(provider="fake", approval="auto"),
        model_client=FakeModelClient(["<final>ok</final>"]),
        workspace=WorkspaceContext.build(str(tmp_path)),
        tools=build_tool_registry(ctx),
        cwd=str(tmp_path),
        tool_context=ctx,
    )
    result = ToolExecutor(agent, approval_policy="auto").execute(
        "quick_test", {"nodeid": "test_a.py"}
    )
    assert result.ok
    assert len(backend.requests) == 1
    assert result.metadata["execution_tier"] == "linux_sandbox"
    assert result.metadata["sandbox_receipt_id"] == "receipt-1"
    assert "rollback_attempted" not in result.metadata


def test_manifest_cannot_enable_sandbox_blocked_tools(tmp_path):
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "tools.yaml").write_text("tools:\n  git_diff: [patcher]\n")
    ctx, _ = context(tmp_path)
    tools = build_repair_canonical_tools(ctx)
    gateway = build_repair_gateway(str(tmp_path), sandbox_mode=True)
    gateway.bind_tools(tools)
    assert tools["git_diff"]["lifecycle"] == "disabled"
    assert not gateway.can_call("patcher", "git_diff")
    assert tools["run_shell"]["execution_tier"] == "linux_sandbox"
