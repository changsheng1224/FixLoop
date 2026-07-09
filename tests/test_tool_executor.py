"""ToolExecutor 7 道闸口单测。"""

import pytest

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

    # Gate 6+7+8: snapshot + execute + diff
    def test_writes_get_snapshot_diff(self, agent):
        executor = ToolExecutor(agent=agent, approval_policy="auto")
        result = executor.execute("write_file", {"path": "new.txt", "content": "created"})
        assert result.metadata["tool_status"] == "success"
        assert "affected_paths" in result.metadata
        assert "new.txt" in result.metadata["affected_paths"]

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
