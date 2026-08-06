"""Native 前缀不得再教 XML function_calls。"""

from __future__ import annotations

from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import ContextManager
from agent_runtime.providers.clients import FakeModelClient, FakeNativeToolClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


def test_build_for_native_prefix_has_no_xml_tool_protocol(temp_workspace):
    config = AgentConfig(provider="fake", max_steps=4, prompt_budget=6000)
    ws = WorkspaceContext.build(str(temp_workspace))
    agent = Agent(
        config=config,
        model_client=FakeNativeToolClient(["<final>ok</final>"]),
        workspace=ws,
    )
    system, user, _meta = ContextManager(agent).build_for_native("fix a bug")
    blob = f"{system}\n{user}"
    # 禁止「示范/推荐」XML 协议（提及禁用标签名可以）
    assert "推荐使用格式B" not in blob
    assert "**1. 工具调用格式**" not in blob
    assert '格式A (JSON): <tool>{"name"' not in blob
    assert '<invoke name="工具名">' not in blob
    assert '<invoke name="read_file">' not in blob
    assert "tool_use" in blob
    assert "仅通过接口提供的 tools" in system


def test_text_path_prefix_still_documents_xml(temp_workspace):
    config = AgentConfig(provider="fake", max_steps=4, prompt_budget=6000)
    ws = WorkspaceContext.build(str(temp_workspace))
    agent = Agent(
        config=config,
        model_client=FakeModelClient(["<final>ok</final>"]),
        workspace=ws,
    )
    prompt, _ = ContextManager(agent).build("fix a bug")
    assert "function_calls" in prompt or "<tool>" in prompt
