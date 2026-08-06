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


def _legacy_prefix_tokens(meta: dict) -> int:
    sections = meta["sections"]
    return (
        sections.get("system", 0)
        + sections.get("tools", 0)
        + sections.get("skills", 0)
        + sections.get("workspace", 0)
    )


class TestSectionOrder:
    def test_build_order_system_before_tools_before_workspace(self, agent):
        agent.session.setdefault("memory", {})["working"] = {"task_summary": "fix bug"}
        cm = ContextManager(agent)
        prompt, meta = cm.build("do it")

        rules_pos = prompt.index("规则")
        tools_pos = prompt.index("可用工具")
        ws_pos = prompt.index("Workspace:")
        mem_pos = prompt.index("fix bug")
        task_pos = prompt.index("当前任务")

        assert rules_pos < tools_pos < ws_pos < mem_pos < task_pos
        assert "system" in meta["sections"]
        assert "tools" in meta["sections"]
        assert "workspace" in meta["sections"]
        assert meta["sections"]["prefix"] == _legacy_prefix_tokens(meta)

    def test_tools_not_in_system_section(self, agent):
        cm = ContextManager(agent)
        assert "可用工具" not in cm._get_system()
        assert "可用工具" in cm._get_tools()
        assert "Workspace:" in cm._get_workspace()

    def test_build_dynamic_context_excludes_stable_sections(self, agent):
        cm = ContextManager(agent)
        dynamic, meta = cm.build_dynamic_context("my task")
        assert "可用工具" not in dynamic
        assert "调用示例" not in dynamic
        assert "Workspace:" in dynamic
        assert "my task" not in dynamic
        assert "request" not in meta["sections"]
        assert "system" not in meta["sections"]
        assert "tools" not in meta["sections"]

    def test_build_for_native_splits_cache_and_dynamic(self, agent):
        cm = ContextManager(agent)
        system, user, meta = cm.build_for_native("run tests")
        assert "可用工具" in system
        assert "调用示例" in user
        assert "Workspace:" not in system
        assert "Workspace:" in user
        assert "当前任务" in user
        assert "run tests" in user
        assert meta["sections"]["prefix"] == _legacy_prefix_tokens(meta)


class TestNativePromptSplit:
    def test_ask_uses_native_system_without_xml_protocol(self, temp_workspace):
        client = FakeNativeToolClient(["<final>done</final>"])
        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=4, prompt_budget=6000),
            model_client=client,
            workspace=ws,
        )
        agent.ask("hello")

        first_prompt = client.prompts[0]
        assert "可用工具" in first_prompt
        assert "Workspace:" in first_prompt
        assert "推荐使用格式B" not in first_prompt
        assert '<invoke name="工具名">' not in first_prompt
        assert "tool_use" in first_prompt
        assert first_prompt.index("可用工具") < first_prompt.index("Workspace:")
