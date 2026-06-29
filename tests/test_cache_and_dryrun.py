"""Prompt Cache + Dry-Run 单测。"""

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


@pytest.fixture
def agent(temp_workspace):
    config = AgentConfig(provider="fake", max_steps=3)
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
        """同一 workspace → 同一 cache key。"""
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
        # FakeClient just ignores cache key
        result = fc.complete("test", prompt_cache_key="abc")
        assert result == "x"
