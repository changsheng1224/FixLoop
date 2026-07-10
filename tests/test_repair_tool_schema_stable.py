"""Repair 工具 schema 集稳定化单测。"""

import pytest

from agent_runtime.prefix_stable import hash_stable_prefix
from agent_runtime.providers.clients import FakeModelClient, FakeNativeToolClient
from agent_runtime.tool_context import ToolContext
from agent_runtime.workspace import WorkspaceContext
from src.agents.factory import create_localizer, create_patcher, create_retriever, create_verifier
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

    def test_all_repair_roles_share_same_tool_registry(self, ctx):
        roles = ("localizer", "retriever", "patcher", "verifier", "baseline")
        registries = [build_repair_agent_tools(ctx, role) for role in roles]
        keys = [tuple(sorted(r.keys())) for r in registries]
        assert len(set(keys)) == 1
        assert keys[0] == REPAIR_CANONICAL_TOOL_NAMES


class TestRepairPrefixHash:
    @pytest.fixture
    def ws(self, temp_workspace):
        return WorkspaceContext.build(str(temp_workspace))

    def test_repair_agents_share_prefix_hash(self, ws):
        client = FakeModelClient(["<final>ok</final>"])
        agents = [
            create_localizer(client, ws, cwd=str(ws.repo_root)),
            create_retriever(client, ws, cwd=str(ws.repo_root)),
            create_patcher(client, ws, cwd=str(ws.repo_root)),
            create_verifier(client, ws, cwd=str(ws.repo_root)),
        ]
        hashes = [a._prefix.hash for a in agents]
        assert len(set(hashes)) == 1
        assert all(len(h) == 64 for h in hashes)

    def test_repair_agents_differ_in_role_text_not_stable(self, ws):
        loc = create_localizer(FakeModelClient(["<final>ok</final>"]), ws)
        pat = create_patcher(FakeModelClient(["<final>ok</final>"]), ws)
        assert loc._prefix.hash == pat._prefix.hash
        assert loc._prefix.stable_text == pat._prefix.stable_text
        assert loc._prefix.role_text != pat._prefix.role_text
        assert "可用工具" in loc._prefix.stable_text
        assert loc._prefix.role_text not in loc._prefix.stable_text

    def test_tool_names_tuple_sorted(self, ws):
        agent = create_localizer(FakeModelClient(["<final>ok</final>"]), ws)
        assert isinstance(agent._tool_names, tuple)
        assert agent._tool_names == REPAIR_CANONICAL_TOOL_NAMES

    def test_stable_hash_includes_tools_not_l2_role(self, ws):
        from agent_runtime.prompt_prefix import cache_stable_text

        agent = create_localizer(FakeModelClient(["<final>ok</final>"]), ws)
        cache = cache_stable_text(
            agent._prefix.stable_system_text,
            agent._prefix.stable_tools_text,
        )
        assert agent._prefix.hash == hash_stable_prefix(cache)
        assert "list_files" in agent._prefix.stable_tools_text
        assert agent._prefix.role_text[:20] in agent._prefix.text


class TestRepairGatewayStillRestricts:
    def test_localizer_cannot_write_via_gateway(self, temp_workspace):
        ws = WorkspaceContext.build(str(temp_workspace))
        agent = create_localizer(FakeModelClient(["<final>ok</final>"]), ws)
        result = agent.execute_tool("write_file", {"path": "x.py", "content": "bad"})
        assert "不可用" in result.content or "permission" in result.content.lower()


class TestRepairNativePromptSplit:
    def test_native_stable_excludes_l2_role_from_cache_block(self, temp_workspace):
        from agent_runtime.prompt_prefix import cache_stable_text

        client = FakeNativeToolClient(["<final>done</final>"])
        ws = WorkspaceContext.build(str(temp_workspace))
        agent = create_localizer(client, ws, cwd=str(temp_workspace))
        role_snippet = agent._prefix.role_text[:30]
        agent.ask("locate bug")
        first = client.prompts[0]
        cache = cache_stable_text(
            agent._prefix.stable_system_text,
            agent._prefix.stable_tools_text,
        )
        assert cache in first
        assert role_snippet in first
        assert role_snippet not in cache
        assert first.index(cache) < first.index(role_snippet)
