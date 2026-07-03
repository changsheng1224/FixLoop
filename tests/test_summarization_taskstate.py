"""对话摘要 + TaskState 集成测试。"""

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import ContextManager
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


@pytest.fixture
def agent(temp_workspace):
    config = AgentConfig(provider="fake", max_steps=3, max_new_tokens=256)
    ws = WorkspaceContext.build(str(temp_workspace))
    client = FakeModelClient(["<final>ok</final>"])
    return Agent(config=config, model_client=client, workspace=ws)


class TestSummarization:
    """对话摘要测试。"""

    def test_summary_not_triggered_for_short_history(self, agent):
        """短历史不触发摘要。"""
        cm = ContextManager(agent)
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result = cm._maybe_summarize_history(history, trigger_tokens=2600)
        assert len(result) == 2  # 原样返回

    def test_summary_triggered_for_long_history(self, agent):
        """长历史触发摘要。"""
        cm = ContextManager(agent)
        # 构造超长历史
        history = []
        for i in range(50):
            history.append({"role": "user", "content": f"question {i}: " + "x" * 200})
            history.append({"role": "tool", "content": "result: " + "y" * 200})

        result = cm._maybe_summarize_history(history, trigger_tokens=100)
        # 超阈值 → 压缩
        assert len(result) < len(history)

    def test_simple_trim_fallback(self, agent):
        """当摘要生成失败时退化为裁剪。"""
        cm = ContextManager(agent)
        history = [{"role": "user", "content": f"msg {i}: " + "x" * 300} for i in range(30)]

        # 使用一个会抛出异常的 fake complete
        original = agent.model_client.complete

        def failing_complete(*a, **kw):
            raise RuntimeError("模拟 API 失败")

        agent.model_client.complete = failing_complete
        try:
            result = cm._maybe_summarize_history(history, trigger_tokens=50)
            assert len(result) <= 8  # 降级为保留最近 8 条
        finally:
            agent.model_client.complete = original

    def test_summary_with_fake_client(self, agent):
        """使用 FakeClient 提供摘要内容。"""
        agent.model_client = FakeModelClient(["Read config.py and tools.py, found Agent class."])
        cm = ContextManager(agent)
        history = [{"role": "user", "content": f"msg {i}: " + "z" * 300} for i in range(20)]

        result = cm._maybe_summarize_history(history, trigger_tokens=50)
        # 有一条 system 角色的摘要
        system_msgs = [h for h in result if h.get("role") == "system"]
        assert len(system_msgs) >= 1
        assert "Earlier summary" in system_msgs[0]["content"]


class TestTaskStateIntegration:
    """TaskState 集成到 AgentLoop 测试。"""

    def test_ask_creates_trace_artifacts(self, temp_workspace):
        """模拟一次 ask() 验证产生工件。"""
        config = AgentConfig(provider="fake", max_steps=2)
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient(["<final>done</final>"])
        agent = Agent(config=config, model_client=client, workspace=ws)

        answer = agent.ask("hello")
        assert answer == "done"

        # 验证 .agent/runs/ 下有工件
        runs_dir = temp_workspace / ".agent" / "runs"
        assert runs_dir.exists()
        run_dirs = list(runs_dir.iterdir())
        assert len(run_dirs) >= 1
        trace_file = run_dirs[0] / "trace.jsonl"
        assert trace_file.exists()

    def test_task_state_after_tool_loop(self, temp_workspace):
        """多步工具调用后 TaskState 状态正确。"""
        config = AgentConfig(provider="fake", max_steps=4, max_new_tokens=256)
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient(
            [
                '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                "<final>all done</final>",
            ]
        )
        agent = Agent(config=config, model_client=client, workspace=ws)
        answer = agent.ask("list files")
        assert "done" in answer

        # 验证 task_state 工件
        runs_dir = temp_workspace / ".agent" / "runs"
        task_state_path = list(runs_dir.iterdir())[0] / "task_state.json"
        import json

        data = json.loads(task_state_path.read_text())
        assert data["status"] == "completed"
        assert data["tool_steps"] == 1
        assert data["stop_reason"] == "final"
