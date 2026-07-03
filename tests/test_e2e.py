"""M1-M4 端到端集成测试：FakeClient 模拟完整多步 ask 流程。"""

import json

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


class TestEndToEnd:
    """完整管线：read → search → write → final → 验证所有模块。"""

    def test_full_pipeline(self, temp_workspace):
        config = AgentConfig(provider="fake", max_steps=5, max_new_tokens=256)
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient(
            [
                '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
                '<tool>{"name":"search","args":{"pattern":"Agent","path":"."}}</tool>',
                "<final>分析完成：项目是一个手写的Agent运行时。</final>",
            ]
        )
        agent = Agent(config=config, model_client=client, workspace=ws)

        answer = agent.ask("分析这个项目")
        assert "Agent" in answer or "运行时" in answer

        # 验证 M3: 记忆
        mem = agent.session["memory"]
        assert "README.md" in mem["working"]["recent_files"]
        assert len(mem["episodic_notes"]) >= 1

        # 验证 M3: trace 工件
        runs_dir = temp_workspace / ".agent" / "runs"
        trace_files = list(runs_dir.glob("*/trace.jsonl"))
        assert len(trace_files) >= 1

        # 验证 M3: checkpoint
        assert len(agent.session.get("checkpoints", [])) >= 1

        # 验证 M4: 模块初始化
        assert agent.semantic_memory is not None
        assert agent.circuit_breaker is not None
        assert agent.quota is not None

        # 验证 M3: task_state
        task_files = list(runs_dir.glob("*/task_state.json"))
        assert len(task_files) >= 1
        data = json.loads(task_files[0].read_text())
        assert data["tool_steps"] == 2
        assert data["status"] == "completed"

    def test_multi_tool_with_memory(self, temp_workspace):
        """多工具调用链 + 记忆累积验证。"""
        config = AgentConfig(provider="fake", max_steps=4, max_new_tokens=256)
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient(
            [
                '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                '<tool>{"name":"read_file","args":{"path":"CLAUDE.md"}}</tool>',
                '<tool>{"name":"search","args":{"pattern":"agent","path":"."}}</tool>',
                "<final>找到相关文件。</final>",
            ]
        )
        agent = Agent(config=config, model_client=client, workspace=ws)
        answer = agent.ask("探索项目")
        assert "相关" in answer

        # 记忆应累积（list + read + search: CLAUDE.md + search note）
        mem = agent.session["memory"]
        assert len(mem["working"]["recent_files"]) >= 1  # read_file → CLAUDE.md
        assert len(mem["episodic_notes"]) >= 1  # search 产生 note
