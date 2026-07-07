"""prompt_prefix 单测：System Prompt 构建、缓存 key、工具签名。"""

from agent_runtime.prompt_prefix import (
    PromptPrefix,
    _tool_signature,
    build_prompt_prefix,
)
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import build_tool_registry


class TestBuildPromptPrefix:
    """build_prompt_prefix 测试。"""

    def test_returns_prompt_prefix_instance(self, temp_workspace):
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        ctx = ToolContext(root=str(temp_workspace))
        registry = build_tool_registry(ctx)
        prefix = build_prompt_prefix(ws, registry)

        assert isinstance(prefix, PromptPrefix)
        assert len(prefix.text) > 500
        assert len(prefix.hash) == 64

    def test_contains_all_sections(self, temp_workspace):
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        ctx = ToolContext(root=str(temp_workspace))
        registry = build_tool_registry(ctx)
        prefix = build_prompt_prefix(ws, registry)

        # 必须包含的关键内容
        assert "规则" in prefix.text
        assert "可用工具" in prefix.text
        assert "list_files" in prefix.text
        assert "read_file" in prefix.text
        assert "search" in prefix.text
        assert "调用示例" in prefix.text
        assert "Workspace:" in prefix.text

    def test_tool_signature_consistent(self, temp_workspace):
        from agent_runtime.workspace import WorkspaceContext

        WorkspaceContext.build(str(temp_workspace))
        ctx = ToolContext(root=str(temp_workspace))
        registry = build_tool_registry(ctx)

        sig1 = _tool_signature(registry)
        sig2 = _tool_signature(registry)
        # 相同工具集 → 相同签名
        assert sig1 == sig2
        assert len(sig1) == 64

    def test_hash_stable_for_same_input(self, temp_workspace):
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        ctx = ToolContext(root=str(temp_workspace))
        registry = build_tool_registry(ctx)

        p1 = build_prompt_prefix(ws, registry)
        p2 = build_prompt_prefix(ws, registry)
        # 相同输入 → 相同 hash（用于 prompt cache）
        assert p1.hash == p2.hash

    def test_l0_filters_prefix_tools_to_enabled_set(self, temp_workspace):
        from agent_runtime.workspace import WorkspaceContext

        ws = WorkspaceContext.build(str(temp_workspace))
        ctx = ToolContext(root=str(temp_workspace))
        registry = build_tool_registry(ctx)
        enabled = {"read_file", "search"}

        prefix = build_prompt_prefix(ws, registry, tool_names=enabled)

        tools_section = prefix.text.split("## 调用示例", 1)[0]
        assert "### read_file" in tools_section
        assert "### search" in tools_section
        assert "### write_file" not in tools_section
        assert "### list_files" not in tools_section
