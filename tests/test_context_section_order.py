"""ContextManager section 顺序与 native 拆分单测。"""

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import ContextManager
from agent_runtime.providers.clients import FakeModelClient, FakeNativeToolClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


@pytest.fixture
def agent(temp_workspace):
    config = AgentConfig(provider="fake", max_steps=4, prompt_budget=6000)
    ws = WorkspaceContext.build(str(temp_workspace))
    client = FakeModelClient(["<final>ok</final>"])
    return Agent(config=config, model_client=client, workspace=ws)


class TestSectionOrder:
    def test_build_order_system_before_workspace_before_memory(self, agent):
        agent.session.setdefault("memory", {})["working"] = {"task_summary": "fix bug"}
        cm = ContextManager(agent)
        prompt, meta = cm.build("do it")

        sys_pos = prompt.index("可用工具")
        ws_pos = prompt.index("Workspace:")
        mem_pos = prompt.index("fix bug")
        task_pos = prompt.index("当前任务")

        assert sys_pos < ws_pos < mem_pos < task_pos
        assert "system" in meta["sections"]
        assert "workspace" in meta["sections"]
        assert meta["sections"]["prefix"] == (
            meta["sections"]["system"] + meta["sections"]["workspace"]
        )

    def test_stable_text_not_in_workspace_section(self, agent):
        cm = ContextManager(agent)
        assert "Workspace:" not in cm._get_system()
        assert "可用工具" in cm._get_system()
        assert "Workspace:" in cm._get_workspace()

    def test_build_dynamic_context_excludes_system_and_task(self, agent):
        cm = ContextManager(agent)
        dynamic, meta = cm.build_dynamic_context("my task")
        assert "可用工具" not in dynamic
        assert "Workspace:" in dynamic
        assert "my task" not in dynamic
        assert "request" not in meta["sections"]
        assert "system" not in meta["sections"]

    def test_build_for_native_splits_stable_and_dynamic(self, agent):
        cm = ContextManager(agent)
        system, user, meta = cm.build_for_native("run tests")
        assert "可用工具" in system
        assert "Workspace:" not in system
        assert "Workspace:" in user
        assert "当前任务" in user
        assert "run tests" in user
        assert meta["sections"]["prefix"] == (
            meta["sections"]["system"] + meta["sections"]["workspace"]
        )


class TestNativePromptSplit:
    def test_ask_uses_stable_system_and_workspace_in_user(self, temp_workspace):
        client = FakeNativeToolClient(["<final>done</final>"])
        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=4, prompt_budget=6000),
            model_client=client,
            workspace=ws,
        )
        agent.ask("hello")

        # FakeNativeToolClient 将 system + user 拼成 complete 的 prompt
        first_prompt = client.prompts[0]
        stable = agent._prefix.stable_text
        assert stable in first_prompt
        assert "Workspace:" in first_prompt
        # stable 在前，workspace 在后（user 侧）
        assert first_prompt.index(stable) < first_prompt.index("Workspace:")
