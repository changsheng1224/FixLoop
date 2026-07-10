"""外置 rules / examples 加载与 prefix hash 单测。"""

import pytest

from agent_runtime.prefix_stable import PrefixStableError, hash_stable_prefix
from agent_runtime.prompt_prefix import cache_stable_text
from agent_runtime.prompt_external import (
    compose_examples,
    compose_rules,
    default_examples_text,
    default_rules_text,
    load_prompt_assets,
)
from agent_runtime.prompt_prefix import build_prompt_prefix, build_repair_agent_prefix
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import build_tool_registry
from agent_runtime.workspace import WorkspaceContext
from src.tools.composite import build_repair_canonical_tools


class TestLoadPromptAssets:
    def test_builtin_when_no_agent_dir(self, temp_workspace):
        assets = load_prompt_assets(temp_workspace)
        assert assets.rules_source == "builtin"
        assert assets.examples_source == "builtin"
        assert assets.rules_text == default_rules_text()
        assert assets.examples_text == default_examples_text()
        assert len(assets.fingerprint) == 64

    def test_repo_rules_overrides_builtin(self, temp_workspace):
        agent_dir = temp_workspace / ".agent"
        agent_dir.mkdir()
        (agent_dir / "rules.md").write_text(
            "## 项目规则\n\n**1. 禁止修改 tests/ 目录**",
            encoding="utf-8",
        )
        assets = load_prompt_assets(temp_workspace)
        assert assets.rules_source == "repo:.agent/rules.md"
        assert "禁止修改 tests" in assets.rules_text
        assert assets.examples_source == "builtin"

    def test_repo_examples_changes_fingerprint(self, temp_workspace):
        agent_dir = temp_workspace / ".agent"
        agent_dir.mkdir()
        base = load_prompt_assets(temp_workspace).fingerprint
        (agent_dir / "examples.md").write_text(
            "### 示例 1: 自定义\n```\n<final>ok</final>\n```",
            encoding="utf-8",
        )
        updated = load_prompt_assets(temp_workspace).fingerprint
        assert base != updated

    def test_repo_rules_rejects_dynamic_fields(self, temp_workspace):
        agent_dir = temp_workspace / ".agent"
        agent_dir.mkdir()
        (agent_dir / "rules.md").write_text("run_id: abc", encoding="utf-8")
        with pytest.raises(PrefixStableError):
            load_prompt_assets(temp_workspace)


class TestComposeRules:
    def test_appends_approval_auto_suffix(self, temp_workspace):
        assets = load_prompt_assets(temp_workspace)
        text = compose_rules(assets, approval="auto")
        assert "自动审批权限" in text


class TestPrefixIntegration:
    def test_custom_rules_in_stable_and_hash(self, temp_workspace):
        agent_dir = temp_workspace / ".agent"
        agent_dir.mkdir()
        (agent_dir / "rules.md").write_text("## 定制规则\n\nAlways be concise.", encoding="utf-8")
        ws = WorkspaceContext.build(str(temp_workspace))
        ctx = ToolContext(root=str(temp_workspace))
        registry = build_tool_registry(ctx)
        prefix = build_prompt_prefix(ws, registry, repo_root=str(temp_workspace))
        assert "Always be concise" in prefix.stable_system_text
        assert prefix.hash == hash_stable_prefix(
            cache_stable_text(prefix.stable_system_text, prefix.stable_tools_text)
        )
        assert prefix.assets_fingerprint != ""

    def test_repair_agents_share_assets_fingerprint(self, temp_workspace):
        from agent_runtime.providers.clients import FakeModelClient
        from src.agents.factory import create_localizer, create_patcher

        ws = WorkspaceContext.build(str(temp_workspace))
        loc = create_localizer(FakeModelClient(["<final>ok</final>"]), ws, cwd=str(temp_workspace))
        pat = create_patcher(FakeModelClient(["<final>ok</final>"]), ws, cwd=str(temp_workspace))
        assert loc._prefix.assets_fingerprint == pat._prefix.assets_fingerprint

    def test_repair_prefix_uses_external_examples(self, temp_workspace):
        agent_dir = temp_workspace / ".agent"
        agent_dir.mkdir()
        (agent_dir / "examples.md").write_text(
            "### 示例 1: Repair custom\n```\n<final>done</final>\n```",
            encoding="utf-8",
        )
        ws = WorkspaceContext.build(str(temp_workspace))
        ctx = ToolContext(root=str(temp_workspace))
        registry = build_repair_canonical_tools(ctx)
        prefix = build_repair_agent_prefix(
            "You are Localizer.",
            ws,
            registry,
            repo_root=str(temp_workspace),
        )
        assert "Repair custom" in prefix.stable_text
