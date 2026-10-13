"""Repair 工具 schema 集稳定化单测。"""

import pytest

from agent_runtime.context_manager import TokenBudget
from agent_runtime.prefix_stable import hash_stable_prefix
from agent_runtime.providers.clients import FakeModelClient, FakeNativeToolClient
from agent_runtime.tool_context import ToolContext
from agent_runtime.workspace import WorkspaceContext
from src.agents.factory import create_patcher
from src.tools.composite import (
    REPAIR_CANONICAL_TOOL_NAMES,
    build_repair_agent_tools,
    build_repair_canonical_tools,
    is_repair_canonical_registry,
)


@pytest.fixture
def ctx(temp_workspace):
    return ToolContext(root=str(temp_workspace))


class TestRepairCanonicalTools:
    def test_canonical_names_are_lexicographically_sorted(self):
        assert list(REPAIR_CANONICAL_TOOL_NAMES) == sorted(REPAIR_CANONICAL_TOOL_NAMES)

    def test_canonical_registry_keys_match_manifest(self, ctx):
        tools = build_repair_canonical_tools(ctx)
        assert tuple(sorted(tools.keys())) == REPAIR_CANONICAL_TOOL_NAMES
        assert is_repair_canonical_registry(tools)

    def test_only_patcher_model_role_is_supported(self, ctx):
        tools = build_repair_agent_tools(ctx, "patcher")
        assert tuple(sorted(tools)) == REPAIR_CANONICAL_TOOL_NAMES
        with pytest.raises(ValueError, match="unsupported repair agent role"):
            build_repair_agent_tools(ctx, "verifier")


class TestRepairPrefixHash:
    @pytest.fixture
    def ws(self, temp_workspace):
        return WorkspaceContext.build(str(temp_workspace))

    def test_repair_agents_share_prefix_hash(self, ws):
        client = FakeModelClient(["<final>ok</final>"])
        agents = [
            create_patcher(client, ws, cwd=str(ws.repo_root)),
            create_patcher(client, ws, cwd=str(ws.repo_root)),
        ]
        hashes = [a._prefix.hash for a in agents]
        assert len(set(hashes)) == 1
        assert all(len(h) == 64 for h in hashes)

    def test_tool_names_tuple_sorted(self, ws):
        agent = create_patcher(FakeModelClient(["<final>ok</final>"]), ws)
        assert isinstance(agent._tool_names, tuple)
        assert agent._tool_names == tuple(
            sorted((*REPAIR_CANONICAL_TOOL_NAMES, "delegate_exploration", "collect_exploration"))
        )
        assert "delegate_exploration" not in agent._prefix.stable_tools_text
        assert "delegate_exploration" in agent._prefix.role_text

    def test_stable_hash_includes_tools_not_l2_role(self, ws):
        from agent_runtime.prompt_prefix import cache_stable_text

        agent = create_patcher(FakeModelClient(["<final>ok</final>"]), ws)
        cache = cache_stable_text(agent._prefix.stable_system_text, agent._prefix.stable_tools_text)
        assert agent._prefix.hash == hash_stable_prefix(cache)
        assert "list_files" in agent._prefix.stable_tools_text
        assert agent._prefix.role_text[:20] in agent._prefix.text


class TestRepairNativePromptSplit:
    def test_native_stable_excludes_l2_role_from_cache_block(self, temp_workspace):
        from agent_runtime.prompt_prefix import cache_stable_text

        client = FakeNativeToolClient(["<final>done</final>"])
        ws = WorkspaceContext.build(str(temp_workspace))
        agent = create_patcher(
            client,
            ws,
            cwd=str(temp_workspace),
            budget=TokenBudget(model="deepseek-v4-pro", provider="deepseek", total_limit=50000),
        )
        agent.config.prompt_budget = 100000
        agent.config.hard_cap = 100000
        role_snippet = agent._prefix.role_text[:30]
        agent.ask("fix bug")
        first = client.prompts[0]
        cache = cache_stable_text(agent._prefix.stable_system_text, agent._prefix.stable_tools_text)
        assert "仅通过接口提供的 tools / tool_use" in first
        assert role_snippet in first
        assert role_snippet not in cache
