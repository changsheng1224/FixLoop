"""Agent 记忆钩子集成测试：update_memory_after_tool 全链路。"""

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


@pytest.fixture
def agent_with_memory(temp_workspace):
    """创建带记忆的 Agent。"""
    config = AgentConfig(provider="fake", max_steps=3)
    ws = WorkspaceContext.build(str(temp_workspace))
    client = FakeModelClient(["<final>ok</final>"])
    return Agent(config=config, model_client=client, workspace=ws)


class TestMemoryHooks:
    """update_memory_after_tool 钩子测试。"""

    def test_read_file_adds_to_memory(self, agent_with_memory):
        agent_with_memory.update_memory_after_tool(
            "read_file",
            {"path": "config.py"},
            "1 | from pydantic import BaseModel\n2 | \n3 | class AgentConfig...",
        )
        mem = agent_with_memory.session["memory"]
        assert "config.py" in mem["working"]["recent_files"]
        assert "config.py" in mem["file_summaries"]
        assert "AgentConfig" in mem["file_summaries"]["config.py"]["summary"]
        assert mem["working"]["evidence_ledger"][0]["path"] == "config.py"

    def test_repeated_read_updates_evidence_duplicate_count(self, agent_with_memory):
        args = {"path": "config.py", "start": 1, "end": 20}
        body = "1 | from pydantic import BaseModel\n2 | class AgentConfig..."
        agent_with_memory.update_memory_after_tool("read_file", args, body)
        agent_with_memory.update_memory_after_tool("read_file", args, body)
        mem = agent_with_memory.session["memory"]
        ledger = mem["working"]["evidence_ledger"]
        assert len(ledger) == 1
        assert ledger[0]["duplicate_count"] == 1

    def test_context_memory_renders_evidence_ledger(self, agent_with_memory):
        from agent_runtime.context_manager import ContextManager

        agent_with_memory.update_memory_after_tool(
            "read_file",
            {"path": "config.py", "start": 1, "end": 20},
            "1 | class AgentConfig:",
        )
        text = ContextManager(agent_with_memory)._get_memory()
        assert "证据账本" in text
        assert "config.py:1-20" in text

    def test_write_file_adds_and_invalidates(self, agent_with_memory):
        # 先建立摘要
        agent_with_memory.update_memory_after_tool(
            "read_file", {"path": "a.py"}, "old content"
        )
        mem = agent_with_memory.session["memory"]
        assert "a.py" in mem["file_summaries"]

        # 再写入
        agent_with_memory.update_memory_after_tool(
            "write_file", {"path": "a.py"}, "已写入 a.py（10 字符）"
        )
        # 文件仍在 recent_files，但摘要已失效
        assert "a.py" in mem["working"]["recent_files"]
        assert "a.py" not in mem["file_summaries"]
        assert mem["working"]["evidence_ledger"][0]["stale"] is True

    def test_shell_error_appends_note(self, agent_with_memory):
        agent_with_memory.update_memory_after_tool(
            "run_shell", {"command": "pytest tests/"}, "exit_code: 1\nstderr:\n1 failed"
        )
        notes = agent_with_memory.session["memory"]["episodic_notes"]
        assert len(notes) == 1
        assert notes[0]["kind"] == "error"
        assert "pytest" in notes[0]["text"]

    def test_shell_success_appends_observation(self, agent_with_memory):
        agent_with_memory.update_memory_after_tool(
            "run_shell", {"command": "echo done"}, "exit_code: 0\nstdout:\ndone"
        )
        notes = agent_with_memory.session["memory"]["episodic_notes"]
        assert len(notes) == 1
        assert notes[0]["kind"] == "observation"

    def test_search_appends_note(self, agent_with_memory):
        agent_with_memory.update_memory_after_tool(
            "search", {"pattern": "Agent"}, "agent_runtime/runtime.py:16: class Agent:"
        )
        notes = agent_with_memory.session["memory"]["episodic_notes"]
        assert len(notes) == 1
        assert any("search" in t for t in notes[0]["tags"])


class TestMemoryFullPipeline:
    """FakeClient 模拟完整 ask() 流程中的记忆累积。"""

    def test_memory_accumulates_across_rounds(self, agent_with_memory):
        """模拟：read_file → write_file → 验证 memory。"""
        agent = agent_with_memory
        # 手动调用钩子模拟工具执行后
        agent.update_memory_after_tool("read_file", {"path": "a.py"}, "content of a.py")
        agent.update_memory_after_tool("read_file", {"path": "b.py"}, "content of b.py")
        agent.update_memory_after_tool("write_file", {"path": "c.py"}, "已写入 c.py（5 字符）")

        mem = agent.session["memory"]
        assert len(mem["working"]["recent_files"]) == 3
        assert "a.py" in mem["file_summaries"]  # read 后摘要保留
        assert "b.py" in mem["file_summaries"]
        assert "c.py" not in mem["file_summaries"]  # write 后摘要失效

    def test_memory_starts_empty(self, agent_with_memory):
        mem = agent_with_memory.session["memory"]
        assert mem["working"]["recent_files"] == []
        assert mem["file_summaries"] == {}
        assert mem["episodic_notes"] == []
