"""prefix 稳定段校验与 cache hash 单测。"""

import hashlib

import pytest

from agent_runtime.prefix_stable import (
    PrefixStableError,
    assert_stable_prefix_clean,
    hash_stable_prefix,
)
from agent_runtime.prompt_prefix import (
    build_custom_system_prefix,
    build_prompt_prefix,
    cache_stable_text,
)
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import build_tool_registry


class _FakeWorkspace:
    def __init__(self, git_status: str):
        self._git_status = git_status

    def text(self) -> str:
        lines = ["Workspace:", "  cwd: /tmp/repo", f"  git_status: {self._git_status}"]
        return "\n".join(lines)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.text().encode()).hexdigest()


class TestAssertStablePrefixClean:
    def test_accepts_normal_stable_text(self):
        assert_stable_prefix_clean("## 核心规则\nYou are an agent.")

    @pytest.mark.parametrize(
        "bad",
        [
            "run_id: abc",
            "session_id=xyz",
            "timestamp: now",
            "nonce: deadbeef",
            "uuid: 550e8400-e29b-41d4-a716-446655440000",
            "built_at: 2026-07-10T05:00:00+00:00",
        ],
    )
    def test_rejects_forbidden_dynamic_fields(self, bad):
        with pytest.raises(PrefixStableError):
            assert_stable_prefix_clean(bad)


class TestHashStablePrefix:
    def test_hash_is_sha256_hex(self):
        h = hash_stable_prefix("stable-only")
        assert len(h) == 64
        assert h == hashlib.sha256(b"stable-only").hexdigest()


class TestBuildPromptPrefixStableHash:
    def _build(self, git_status: str):
        ws = _FakeWorkspace(git_status)
        ctx = ToolContext(root=".")
        registry = build_tool_registry(ctx)
        return build_prompt_prefix(ws, registry), ws

    def test_stable_text_excludes_workspace(self, temp_workspace):
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        ctx = ToolContext(root=str(temp_workspace))
        registry = build_tool_registry(ctx)
        prefix = build_prompt_prefix(ws, registry)

        assert "Workspace:" in prefix.text
        assert "Workspace:" not in prefix.stable_text
        assert prefix.workspace_text
        assert "Workspace:" in prefix.workspace_text
        assert prefix.text.endswith(prefix.stable_text) is False
        assert prefix.stable_text in prefix.text

    def test_hash_unchanged_when_workspace_volatile_changes(self):
        p1, _ = self._build(" M foo.py")
        p2, _ = self._build(" M bar.py")

        assert p1.hash == p2.hash
        assert p1.stable_text == p2.stable_text
        assert p1.text != p2.text
        assert p1.workspace_fingerprint != p2.workspace_fingerprint

    def test_hash_matches_stable_text(self, temp_workspace):
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        ctx = ToolContext(root=str(temp_workspace))
        registry = build_tool_registry(ctx)
        prefix = build_prompt_prefix(ws, registry)

        assert prefix.hash == hash_stable_prefix(
            cache_stable_text(prefix.stable_system_text, prefix.stable_tools_text)
        )

    def test_no_built_at_field(self, temp_workspace):
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        ctx = ToolContext(root=str(temp_workspace))
        registry = build_tool_registry(ctx)
        prefix = build_prompt_prefix(ws, registry)

        assert not hasattr(prefix, "built_at")


class TestBuildCustomSystemPrefix:
    def test_l2_system_prompt_gets_stable_hash(self):
        ws = _FakeWorkspace("clean")
        prefix = build_custom_system_prefix("You are Localizer.", ws)

        assert prefix.hash == hash_stable_prefix("You are Localizer.")
        assert prefix.stable_text == "You are Localizer."
        assert "Workspace:" in prefix.text
        assert len(prefix.hash) == 64

    def test_l2_rejects_dynamic_fields(self):
        ws = _FakeWorkspace("clean")
        with pytest.raises(PrefixStableError):
            build_custom_system_prefix("rules\nrun_id: x", ws)


class TestBuildPrefixHashes:
    def test_returns_segment_keys(self, temp_workspace):
        from agent_runtime.prompt_prefix import build_prefix_hashes
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        ctx = ToolContext(root=str(temp_workspace))
        registry = build_tool_registry(ctx)
        prefix = build_prompt_prefix(ws, registry)

        hashes = build_prefix_hashes(prefix)
        for key in (
            "system",
            "tools",
            "skills",
            "cache_key",
            "role",
            "tool_signature",
            "assets_fingerprint",
            "workspace_fingerprint",
        ):
            assert key in hashes

    def test_cache_key_matches_prefix_hash(self, temp_workspace):
        from agent_runtime.prompt_prefix import build_prefix_hashes
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        ctx = ToolContext(root=str(temp_workspace))
        registry = build_tool_registry(ctx)
        prefix = build_prompt_prefix(ws, registry)

        hashes = build_prefix_hashes(prefix)
        assert hashes["cache_key"] == prefix.hash
        assert hashes["cache_key"] == hash_stable_prefix(
            cache_stable_text(prefix.stable_system_text, prefix.stable_tools_text)
        )

    def test_segment_hashes_match_stable_text(self, temp_workspace):
        from agent_runtime.prompt_prefix import build_prefix_hashes
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        ctx = ToolContext(root=str(temp_workspace))
        registry = build_tool_registry(ctx)
        prefix = build_prompt_prefix(ws, registry)

        hashes = build_prefix_hashes(prefix)
        assert hashes["system"] == hash_stable_prefix(prefix.stable_system_text)
        assert hashes["tools"] == hash_stable_prefix(prefix.stable_tools_text)
        assert hashes["skills"] == hash_stable_prefix(prefix.stable_skills_text)

    def test_workspace_change_does_not_change_cache_segments(self):
        from agent_runtime.prompt_prefix import build_prefix_hashes

        ws1 = _FakeWorkspace(" M foo.py")
        ws2 = _FakeWorkspace(" M bar.py")
        ctx = ToolContext(root=".")
        registry = build_tool_registry(ctx)
        p1 = build_prompt_prefix(ws1, registry)
        p2 = build_prompt_prefix(ws2, registry)

        h1 = build_prefix_hashes(p1)
        h2 = build_prefix_hashes(p2)
        assert h1["cache_key"] == h2["cache_key"]
        assert h1["system"] == h2["system"]
        assert h1["tools"] == h2["tools"]
        assert h1["skills"] == h2["skills"]
        assert h1["workspace_fingerprint"] != h2["workspace_fingerprint"]

    def test_examples_change_updates_skills_not_cache_key(self, temp_workspace):
        from agent_runtime.prompt_prefix import build_prefix_hashes
        from agent_runtime.workspace import WorkspaceContext

        agent_dir = temp_workspace / ".agent"
        agent_dir.mkdir()
        ws = WorkspaceContext.build(str(temp_workspace))
        ctx = ToolContext(root=str(temp_workspace))
        registry = build_tool_registry(ctx)

        base = build_prompt_prefix(ws, registry, repo_root=str(temp_workspace))
        base_hashes = build_prefix_hashes(base)

        (agent_dir / "examples.md").write_text(
            "### 示例 1: Custom\n```\n<final>ok</final>\n```",
            encoding="utf-8",
        )
        updated = build_prompt_prefix(ws, registry, repo_root=str(temp_workspace))
        updated_hashes = build_prefix_hashes(updated)

        assert base_hashes["cache_key"] == updated_hashes["cache_key"]
        assert base_hashes["skills"] != updated_hashes["skills"]
        assert base_hashes["assets_fingerprint"] != updated_hashes["assets_fingerprint"]
