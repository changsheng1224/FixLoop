"""ToolExecutor 7 道闸口单测。"""

import pytest
from pathlib import Path

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.tool_executor import ToolExecutionResult, ToolExecutor


@pytest.fixture
def config():
    return AgentConfig(provider="fake", max_steps=4, approval="auto")


@pytest.fixture
def agent(config, workspace):
    client = FakeModelClient(["<final>ok</final>"])
    return Agent(config=config, model_client=client, workspace=workspace)


@pytest.fixture
def executor(agent):
    return ToolExecutor(agent=agent, approval_policy="auto")


class TestToolExecutionResult:
    """ToolExecutionResult dataclass 测试。"""

    def test_success_result(self):
        result = ToolExecutionResult(
            content="done",
            metadata={"tool_status": "success"},
        )
        assert result.content == "done"
        assert result.metadata["tool_status"] == "success"

    def test_rejected_result(self):
        result = ToolExecutionResult(
            content="Error: rejected",
            metadata={"tool_status": "rejected", "tool_error_code": "allowed_tools"},
        )
        assert "rejected" in result.metadata["tool_status"]


class TestToolExecutorGates:
    """闸口逐道测试。"""

    # Gate 1: allowed_tools
    def test_rejects_non_allowed_tool(self, executor):
        result = executor.execute("non_existent_tool", {})
        assert "rejected" in result.metadata["tool_status"]
        assert result.metadata["tool_error_code"] == "allowed_tools"

    # Gate 2: tool existence (covered by gate 1 + tools registry)

    # Gate 3: parameter validation
    def test_rejects_invalid_params(self, executor):
        # read_file 缺少必填 path
        result = executor.execute("read_file", {})
        assert "rejected" in result.metadata["tool_status"]

    # Gate 4: duplicate detection
    def test_detects_duplicate_calls(self, executor, agent):
        # 注入历史：连续 2 次相同调用
        agent.record(
            {
                "role": "assistant",
                "content": "calling tool",
                "tool_name": "list_files",
                "tool_args": {"path": "."},
            }
        )
        agent.record(
            {
                "role": "assistant",
                "content": "calling tool",
                "tool_name": "list_files",
                "tool_args": {"path": "."},
            }
        )
        result = executor.execute("list_files", {"path": "."})
        assert "rejected" in result.metadata["tool_status"]
        assert result.metadata["tool_error_code"] == "duplicate"

    def test_allows_non_duplicate(self, executor, agent):
        agent.record({"tool_name": "list_files", "tool_args": {"path": "."}})
        agent.record({"tool_name": "read_file", "tool_args": {"path": "x.py"}})
        result = executor.execute("list_files", {"path": "."})
        # 最近 2 次不是相同调用 → 允许
        assert result.metadata["tool_status"] == "success"

    # Gate 5: approval
    def test_approval_auto_allows(self, agent):
        executor_auto = ToolExecutor(agent=agent, approval_policy="auto")
        result = executor_auto.execute("write_file", {"path": "t.txt", "content": "x"})
        assert result.metadata["tool_status"] == "success"

    def test_approval_never_denies(self, agent):
        executor_never = ToolExecutor(agent=agent, approval_policy="never")
        result = executor_never.execute("write_file", {"path": "t.txt", "content": "x"})
        assert "rejected" in result.metadata["tool_status"]
        assert result.metadata["tool_error_code"] == "approval_denied"
        assert result.metadata["rejection_layer"] == "executor"
        assert result.metadata["gate_id"] == 7

    # Gate 6+7+8: snapshot + execute + diff
    def test_writes_get_snapshot_diff(self, agent):
        executor = ToolExecutor(agent=agent, approval_policy="auto")
        result = executor.execute("write_file", {"path": "new.txt", "content": "created"})
        assert result.metadata["tool_status"] == "success"
        assert "affected_paths" in result.metadata
        assert "new.txt" in result.metadata["affected_paths"]

    def test_gate3_path_escape_rejected(self, executor):
        result = executor.execute("read_file", {"path": "../outside.txt"})
        assert result.metadata["gate_id"] == 3
        assert result.metadata["tool_error_code"] == "path_escape"
        assert result.metadata["tool_status"] == "rejected"

    def test_readonly_tool_no_snapshot(self, agent):
        executor = ToolExecutor(agent=agent, approval_policy="auto")
        result = executor.execute("list_files", {"path": "."})
        assert result.metadata["tool_status"] == "success"
        # 只读工具不做快照
        assert "affected_paths" not in result.metadata

    def test_patch_file_includes_preview_metadata(self, agent, temp_workspace):
        (temp_workspace / "p.py").write_text("x = 1\n")
        executor = ToolExecutor(agent=agent, approval_policy="auto")
        result = executor.execute(
            "patch_file",
            {"path": "p.py", "old_text": "x = 1", "new_text": "x = 2"},
        )
        assert result.metadata["tool_status"] == "success"
        preview = result.metadata.get("patch_preview")
        assert preview is not None
        assert preview["hunk_count"] == 1
        assert "preview_text" in preview

    def test_patch_file_approval_shows_preview(self, agent, temp_workspace, monkeypatch):
        (temp_workspace / "a.py").write_text("hello\n")
        executor = ToolExecutor(agent=agent, approval_policy="ask")
        prompts: list[str] = []

        def fake_input(prompt):
            prompts.append(prompt)
            return "n"

        monkeypatch.setattr("builtins.input", fake_input)
        result = executor.execute(
            "patch_file",
            {"path": "a.py", "old_text": "hello", "new_text": "world"},
        )
        assert result.metadata["tool_error_code"] == "approval_denied"
        assert prompts
        assert "预览" in prompts[0]
        assert "patch_preview" in result.metadata


class TestApproval:
    """approve() 方法测试。"""

    def test_approve_auto(self, agent):
        executor = ToolExecutor(agent=agent, approval_policy="auto")
        assert executor._approve("write_file", {}) is True

    def test_approve_never(self, agent):
        executor = ToolExecutor(agent=agent, approval_policy="never")
        assert executor._approve("write_file", {}) is False


class TestExecutionTier:
    """execution_tier 注入与透传测试。"""

    def test_host_tool_has_tier_in_metadata(self, agent):
        executor = ToolExecutor(agent=agent, approval_policy="auto")
        result = executor.execute("list_files", {"path": "."})
        assert result.metadata["tool_status"] == "success"
        assert result.metadata.get("execution_tier") == "host"

    def test_run_shell_is_denied_by_gate7(self, agent):
        executor = ToolExecutor(agent=agent, approval_policy="auto")
        result = executor.execute("run_shell", {"command": "echo hello", "timeout": 5})
        # Gate 7 deny tier → run_shell 被禁止
        assert result.metadata["tool_status"] == "rejected"
        assert result.metadata.get("gate_id") == 7

    def test_write_file_is_host_tier(self, agent):
        executor = ToolExecutor(agent=agent, approval_policy="auto")
        result = executor.execute("write_file", {"path": "t.txt", "content": "x"})
        assert result.metadata["tool_status"] == "success"
        assert result.metadata.get("execution_tier") == "host"

    def test_tool_spec_has_execution_tier(self, agent):
        for name in ("list_files", "read_file", "search", "write_file", "patch_file", "run_shell"):
            spec = agent.tools.get(name)
            assert spec is not None, f"missing tool: {name}"
            assert spec.get("execution_tier") == "host", f"{name} should be host tier"

    def test_rejected_tool_has_no_tier_in_metadata(self, executor):
        """被 Gate 拒绝的工具不执行，metadata 中不含 execution_tier。"""
        result = executor.execute("non_existent_tool", {})
        assert result.metadata["tool_status"] == "rejected"
        assert "execution_tier" not in result.metadata

    def test_execution_tier_in_trace_public_keys(self):
        from agent_runtime.tool_rejection import TOOL_TRACE_PUBLIC_KEYS

        assert "execution_tier" in TOOL_TRACE_PUBLIC_KEYS


# ---------------------------------------------------------------------------
# Gate 5 语义 duplicate（V1.4-Bonus11a）
# ---------------------------------------------------------------------------


class TestGate5SemanticDuplicate:
    def test_read_tool_same_path_different_args_duplicate(self):
        """read_file 相同 path 不同 start → 语义重复。"""
        agent = _make_agent()
        agent.session["history"] = [
            {"tool_name": "read_file", "tool_args": {"path": "app.py", "start": 1, "end": 50}},
            {"tool_name": "read_file", "tool_args": {"path": "app.py", "start": 51, "end": 100}},
        ]
        exe = ToolExecutor(agent, approval_policy="auto")
        assert exe._is_duplicate("read_file", {"path": "app.py", "start": 101, "end": 150})

    def test_read_tool_different_path_not_duplicate(self):
        """read_file 不同 path → 不重复。"""
        agent = _make_agent()
        agent.session["history"] = [
            {"tool_name": "read_file", "tool_args": {"path": "app.py"}},
            {"tool_name": "read_file", "tool_args": {"path": "utils.py"}},
        ]
        exe = ToolExecutor(agent, approval_policy="auto")
        assert not exe._is_duplicate("read_file", {"path": "config.py"})

    def test_write_tool_exact_match_duplicate(self):
        """write_file 相同 args → 精确重复。"""
        agent = _make_agent()
        agent.session["history"] = [
            {"tool_name": "write_file",
             "tool_args": {"path": "app.py", "content": "x=1"}},
            {"tool_name": "write_file",
             "tool_args": {"path": "app.py", "content": "x=1"}},
        ]
        exe = ToolExecutor(agent, approval_policy="auto")
        assert exe._is_duplicate("write_file", {"path": "app.py", "content": "x=1"})

    def test_write_tool_different_content_not_duplicate(self):
        """write_file 相同 path 不同 content → 不重复。"""
        agent = _make_agent()
        agent.session["history"] = [
            {"tool_name": "write_file",
             "tool_args": {"path": "app.py", "content": "x=1"}},
            {"tool_name": "write_file",
             "tool_args": {"path": "app.py", "content": "x=2"}},
        ]
        exe = ToolExecutor(agent, approval_policy="auto")
        assert not exe._is_duplicate("write_file", {"path": "app.py", "content": "x=3"})

    def test_less_than_two_calls_not_duplicate(self):
        agent = _make_agent()
        agent.session["history"] = [
            {"tool_name": "read_file", "tool_args": {"path": "app.py"}},
        ]
        exe = ToolExecutor(agent, approval_policy="auto")
        assert not exe._is_duplicate("read_file", {"path": "app.py"})

    def test_search_same_path_different_pattern_duplicate(self):
        """search 相同 path → 语义重复。"""
        agent = _make_agent()
        agent.session["history"] = [
            {"tool_name": "search", "tool_args": {"path": "src", "pattern": "foo"}},
            {"tool_name": "search", "tool_args": {"path": "src", "pattern": "bar"}},
        ]
        exe = ToolExecutor(agent, approval_policy="auto")
        assert exe._is_duplicate("search", {"path": "src", "pattern": "baz"})


# ---------------------------------------------------------------------------
# Gate 7 分级审批（V1.4-Bonus11b）
# ---------------------------------------------------------------------------


class TestGate7ApprovalTiers:
    def test_read_tools_are_auto(self):
        for name in ToolExecutor._READ_TOOLS_FOR_APPROVAL:
            assert ToolExecutor._approval_tier(name) == ToolExecutor._APPROVAL_TIER_AUTO, name

    def test_write_tools_are_ask(self):
        for name in ToolExecutor._ASK_TOOLS:
            assert ToolExecutor._approval_tier(name) == ToolExecutor._APPROVAL_TIER_ASK, name

    def test_run_shell_is_deny(self):
        assert ToolExecutor._approval_tier("run_shell") == ToolExecutor._APPROVAL_TIER_DENY

    def test_unknown_tool_defaults_to_ask(self):
        assert ToolExecutor._approval_tier("unknown_tool") == ToolExecutor._APPROVAL_TIER_ASK

    def test_auto_tool_executes_without_approval(self):
        """读类工具 Gate 7 自动通过（不要求审批）。"""
        agent = _make_agent()
        (Path(agent._cwd) / "app.py").write_text("x=1\n", encoding="utf-8")
        exe = ToolExecutor(agent, approval_policy="ask")  # policy=ask 但读类应 auto
        result = exe.execute_gated("read_file", {"path": "app.py"})
        assert "Error" not in result.content or "审批" not in result.content

    def test_write_file_asks_approval(self):
        """写类工具在 ask policy 下要求审批（stdin 不可用 → 被拒）。"""
        agent = _make_agent()
        (Path(agent._cwd) / "app.py").write_text("x=1\n", encoding="utf-8")
        exe = ToolExecutor(agent, approval_policy="ask")
        # stdin is not available in test → approval denied
        result = exe.execute_gated("write_file", {"path": "app.py", "content": "x=2\n"})
        assert result.metadata.get("tool_status") == "rejected"
        assert result.metadata.get("gate_id") == 7

    def test_deny_tool_rejected(self):
        """deny 工具直接拒绝。"""
        agent = _make_agent()
        exe = ToolExecutor(agent, approval_policy="auto")
        result = exe.execute_gated("run_shell", {"command": "echo hi"})
        assert result.metadata.get("gate_id") == 7
        assert "禁止执行" in result.content


# ---------------------------------------------------------------------------
# Gate 5.5 死循环检测（V1.5-Bonus1）
# ---------------------------------------------------------------------------


class TestLoopDetection:
    def test_single_call_no_detection(self, agent):
        exe = ToolExecutor(agent, approval_policy="auto")
        result = exe.execute_gated("read_file", {"path": "app.py"})
        assert result.metadata.get("tool_error_code") != "loop_detected"

    def test_three_same_calls_triggers(self, agent):
        """连续 3 次相同调用 → loop_detected。"""
        agent.config.loop_detect_threshold = 3
        exe = ToolExecutor(agent, approval_policy="auto")
        args = {"path": "app.py"}
        exe.execute_gated("read_file", args)
        exe.execute_gated("read_file", args)
        result = exe.execute_gated("read_file", args)
        assert result.metadata.get("tool_error_code") == "loop_detected"

    def test_different_args_no_detection(self, agent):
        agent.config.loop_detect_threshold = 3
        exe = ToolExecutor(agent, approval_policy="auto")
        exe.execute_gated("read_file", {"path": "app.py"})
        exe.execute_gated("read_file", {"path": "utils.py"})
        exe.execute_gated("read_file", {"path": "config.py"})
        # 不同 path → 不触发
        result = exe.execute_gated("read_file", {"path": "app.py"})
        assert result.metadata.get("tool_error_code") != "loop_detected"

    def test_threshold_zero_disables(self, agent):
        agent.config.loop_detect_threshold = 0
        exe = ToolExecutor(agent, approval_policy="auto")
        args = {"path": "app.py"}
        for _ in range(5):
            result = exe.execute_gated("read_file", args)
        assert result.metadata.get("tool_error_code") != "loop_detected"

    def test_interleaved_tool_resets_window(self, agent):
        agent.config.loop_detect_threshold = 3
        exe = ToolExecutor(agent, approval_policy="auto")
        exe.execute_gated("read_file", {"path": "app.py"})
        exe.execute_gated("read_file", {"path": "app.py"})
        exe.execute_gated("search", {"pattern": "TODO"})  # 不同工具
        exe.execute_gated("read_file", {"path": "app.py"})
        # 窗口内只有 2 次 read app.py → 不触发
        result = exe.execute_gated("read_file", {"path": "app.py"})
        assert result.metadata.get("tool_error_code") != "loop_detected"


def _make_agent():
    from agent_runtime.workspace import WorkspaceContext
    import tempfile

    tmp = tempfile.mkdtemp()
    ws = WorkspaceContext.build(tmp)
    return Agent(
        config=AgentConfig(approval="auto"),
        model_client=FakeModelClient(outputs=["<final>ok</final>"]),
        workspace=ws,
        cwd=tmp,
    )
