"""Prompt Cache + Dry-Run 单测。"""

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.tool_executor import ToolExecutor
from agent_runtime.workspace import WorkspaceContext


@pytest.fixture
def agent(temp_workspace):
    config = AgentConfig(provider="fake", model="deepseek-v4-pro", max_steps=3, prompt_budget=6000)
    ws = WorkspaceContext.build(str(temp_workspace))
    client = FakeModelClient(["<final>ok</final>"])
    return Agent(config=config, model_client=client, workspace=ws)


class TestPromptCache:
    """Prompt Cache 测试。"""

    def test_cache_key_in_metadata(self, agent):
        from agent_runtime.context_manager import ContextManager

        cm = ContextManager(agent)
        _, meta = cm.build("hello")
        assert "prompt_cache_key" in meta
        assert len(meta["prompt_cache_key"]) == 64

    def test_cache_key_stable(self, agent):
        from agent_runtime.context_manager import ContextManager

        cm = ContextManager(agent)
        _, meta1 = cm.build("q1")
        _, meta2 = cm.build("q2")
        assert meta1["prompt_cache_key"] == meta2["prompt_cache_key"]

    def test_client_supports_cache(self):
        from agent_runtime.providers.clients import (
            AnthropicCompatibleModelClient,
            FakeModelClient,
        )

        ac = AnthropicCompatibleModelClient(model="x", base_url="http://x", api_key="x")
        assert ac.supports_prompt_cache is True

        fc = FakeModelClient(["x"])
        result = fc.complete("test", prompt_cache_key="abc")
        assert result == "x"


class TestDryRun:
    """Dry-Run 模式测试。"""

    def test_dry_run_returns_plan(self, agent):
        executor = ToolExecutor(agent=agent, approval_policy="auto", dry_run=True)
        result = executor.execute("write_file", {"path": "t.txt", "content": "hi"})
        assert "[DRY RUN]" in result.content
        assert result.metadata["dry_run"] is True

    def test_dry_run_no_side_effects(self, agent, temp_workspace):
        executor = ToolExecutor(agent=agent, approval_policy="auto", dry_run=True)
        executor.execute("write_file", {"path": "t.txt", "content": "hi"})
        # 文件不应被创建
        assert not (temp_workspace / "t.txt").exists()

    def test_normal_mode_writes_file(self, agent, temp_workspace):
        executor = ToolExecutor(agent=agent, approval_policy="auto", dry_run=False)
        executor.execute("write_file", {"path": "t.txt", "content": "hi"})
        assert (temp_workspace / "t.txt").exists()

    def test_dry_run_skip_approval_check(self, agent):
        """dry_run 时应跳过审批（不修改文件）。"""
        executor = ToolExecutor(agent=agent, approval_policy="never", dry_run=True)
        result = executor.execute("write_file", {"path": "x.txt", "content": "y"})
        # 即使 approval=never，dry_run 也应该成功返回计划
        assert "[DRY RUN]" in result.content
