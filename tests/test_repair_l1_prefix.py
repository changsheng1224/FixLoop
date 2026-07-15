"""Repair L1 prefix bundle: shared stable segment across repair phases."""

from __future__ import annotations

from agent_runtime.prompt_prefix import (
    build_repair_l1_prefix,
    cache_stable_text,
    compose_repair_prefix,
)
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.tool_context import ToolContext
from agent_runtime.workspace import WorkspaceContext
from src.agents.factory import create_localizer, create_patcher
from src.repair_factory import wire_orchestrator
from src.tools.composite import build_repair_canonical_tools


class TestBuildRepairL1Prefix:
    def test_deterministic_for_same_workspace(self, temp_workspace):
        ws = WorkspaceContext.build(str(temp_workspace))
        ctx = ToolContext(root=str(temp_workspace))
        tools = build_repair_canonical_tools(ctx)
        l1a = build_repair_l1_prefix(ws, tools, repo_root=str(temp_workspace))
        l1b = build_repair_l1_prefix(ws, tools, repo_root=str(temp_workspace))
        assert l1a.hash == l1b.hash
        assert len(l1a.hash) == 64

    def test_dry_run_changes_stable_system_not_cache_tools_only(self, temp_workspace):
        ws = WorkspaceContext.build(str(temp_workspace))
        ctx = ToolContext(root=str(temp_workspace))
        tools = build_repair_canonical_tools(ctx)
        normal = build_repair_l1_prefix(ws, tools, dry_run=False, repo_root=str(temp_workspace))
        dry = build_repair_l1_prefix(ws, tools, dry_run=True, repo_root=str(temp_workspace))
        assert "演习模式" in dry.stable_system_text
        assert "演习模式" not in normal.stable_system_text
        assert dry.hash != normal.hash


class TestComposeRepairPrefix:
    def test_role_differs_hash_unchanged(self, temp_workspace):
        ws = WorkspaceContext.build(str(temp_workspace))
        ctx = ToolContext(root=str(temp_workspace))
        tools = build_repair_canonical_tools(ctx)
        l1 = build_repair_l1_prefix(ws, tools, repo_root=str(temp_workspace))
        loc = compose_repair_prefix(l1, "LOCALIZER ROLE")
        pat = compose_repair_prefix(l1, "PATCHER ROLE")
        assert loc.hash == pat.hash == l1.hash
        assert loc.role_text != pat.role_text
        assert loc.stable_text == pat.stable_text

    def test_hash_matches_cache_stable_text(self, temp_workspace):
        ws = WorkspaceContext.build(str(temp_workspace))
        ctx = ToolContext(root=str(temp_workspace))
        tools = build_repair_canonical_tools(ctx)
        l1 = build_repair_l1_prefix(ws, tools, repo_root=str(temp_workspace))
        from agent_runtime.prefix_stable import hash_stable_prefix

        expected = hash_stable_prefix(
            cache_stable_text(l1.stable_system_text, l1.stable_tools_text)
        )
        assert l1.hash == expected


class TestWireOrchestratorSharedL1:
    def test_all_agents_share_l1_hash_via_factory(self, temp_workspace):
        client = FakeModelClient(["<final>ok</final>"])
        orch = wire_orchestrator(client, str(temp_workspace), dry_run=True)
        agents = [orch.localizer, orch.retriever, orch.patcher]
        hashes = {a._prefix.hash for a in agents if a is not None}
        assert len(hashes) == 1
        assert orch.l1_prompt_cache_key == next(iter(hashes))
        assert orch.localizer.dry_run is True
        assert "演习模式" in orch.localizer._prefix.stable_system_text

    def test_factory_agents_accept_injected_l1(self, temp_workspace):
        ws = WorkspaceContext.build(str(temp_workspace))
        ctx = ToolContext(root=str(temp_workspace))
        tools = build_repair_canonical_tools(ctx)
        l1 = build_repair_l1_prefix(ws, tools, repo_root=str(temp_workspace))
        client = FakeModelClient(["<final>ok</final>"])
        loc = create_localizer(client, ws, cwd=str(temp_workspace), l1_prefix=l1)
        pat = create_patcher(client, ws, cwd=str(temp_workspace), l1_prefix=l1)
        assert loc._prefix.hash == pat._prefix.hash == l1.hash
