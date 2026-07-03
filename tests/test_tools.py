"""工具执行函数单测：list_files, read_file, search, 路径逃逸检测。"""

import pytest

from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import (
    build_tool_registry,
    legal_tool_names,
    tool_list_files,
    tool_read_file,
    tool_search,
)


@pytest.fixture
def ctx(temp_workspace):
    """基于临时 git 仓库的 ToolContext。"""
    return ToolContext(root=str(temp_workspace))


class TestListFiles:
    """list_files 工具测试。"""

    def test_lists_directory(self, ctx, temp_workspace):
        result = tool_list_files(ctx, {"path": "."})
        assert "[F] README.md" in result
        assert "[F] pyproject.toml" in result

    def test_nonexistent_directory(self, ctx):
        result = tool_list_files(ctx, {"path": "nonexistent"})
        assert "Error" in result


class TestReadFile:
    """read_file 工具测试。"""

    def test_reads_file_with_line_numbers(self, ctx):
        result = tool_read_file(ctx, {"path": "README.md"})
        assert "# Test Project" in result
        assert "1 |" in result  # 行号前缀

    def test_missing_path(self, ctx):
        result = tool_read_file(ctx, {})
        assert "Error" in result
        assert "path" in result

    def test_nonexistent_file(self, ctx):
        result = tool_read_file(ctx, {"path": "ghost.py"})
        assert "Error" in result

    def test_line_range(self, ctx, temp_workspace):
        # 创建一个多行文件
        (temp_workspace / "multiline.py").write_text("line1\nline2\nline3\nline4\nline5\n")
        result = tool_read_file(ctx, {"path": "multiline.py", "start": 2, "end": 3})
        assert "2 | line2" in result
        assert "3 | line3" in result
        assert "1 |" not in result
        assert "4 |" not in result


class TestSearch:
    """search 工具测试。"""

    def test_search_finds_pattern(self, ctx):
        result = tool_search(ctx, {"pattern": "Test Project", "path": "."})
        assert "README.md" in result

    def test_search_no_match(self, ctx):
        result = tool_search(ctx, {"pattern": "xyzzy_not_found_42", "path": "."})
        assert "无匹配" in result

    def test_missing_pattern(self, ctx):
        result = tool_search(ctx, {})
        assert "Error" in result
        assert "pattern" in result

    def test_nonexistent_path(self, ctx):
        result = tool_search(ctx, {"pattern": "test", "path": "ghost_dir"})
        assert "Error" in result

    def test_context_lines_shows_surrounding(self, ctx, temp_workspace):
        """context_lines 显示匹配行前后的上下文。"""
        (temp_workspace / "data.txt").write_text("line A\nline B\nTODO fix this\nline D\nline E\n")
        result = tool_search(
            ctx, {"pattern": "TODO", "path": str(temp_workspace), "context_lines": 1}
        )
        assert "TODO fix this" in result
        assert "line B" in result
        assert "line D" in result
        assert "line A" not in result
        assert "line E" not in result

    def test_context_lines_zero(self, ctx, temp_workspace):
        """context_lines=0 时只显示匹配行。"""
        (temp_workspace / "data.txt").write_text("a\nb TODO here\nc\n")
        result = tool_search(
            ctx, {"pattern": "TODO", "path": str(temp_workspace), "context_lines": 0}
        )
        assert "TODO" in result
        assert "b TODO here" in result
        assert "line A" not in result and "\na\n" not in result


class TestToolRegistry:
    """build_tool_registry 测试。"""

    def test_registry_contains_readonly_tools(self, ctx):
        registry = build_tool_registry(ctx)
        assert "list_files" in registry
        assert "read_file" in registry
        assert "search" in registry

    def test_readonly_tools_are_marked_safe(self, ctx):
        registry = build_tool_registry(ctx)
        readonly = {"list_files", "read_file", "search"}
        for name in readonly:
            assert name in registry, f"{name} 缺失"
            assert registry[name]["risky"] is False, f"{name} 应为安全工具"

    def test_write_tools_are_marked_risky(self, ctx):
        registry = build_tool_registry(ctx)
        risky = {"write_file", "patch_file"}
        for name in risky:
            assert name in registry, f"{name} 缺失"
            assert registry[name]["risky"] is True, f"{name} 应为高风险工具"

    def test_tools_have_schema(self, ctx):
        registry = build_tool_registry(ctx)
        for name, spec in registry.items():
            assert "schema" in spec, f"{name} 缺少 schema"
            assert isinstance(spec["schema"], dict)

    def test_tools_runnable(self, ctx):
        """验证工具可以被调用。"""
        registry = build_tool_registry(ctx)
        result = registry["list_files"]["run"]({"path": "."})
        assert "README.md" in result

    def test_legal_tool_names(self, ctx):
        registry = build_tool_registry(ctx)
        names = legal_tool_names(registry)
        expected = {"list_files", "read_file", "search", "write_file", "patch_file", "run_shell"}
        assert names == expected


class TestPathEscape:
    """路径逃逸检测测试。"""

    def test_parent_directory_escape(self, temp_workspace):
        ctx = ToolContext(root=str(temp_workspace))
        with pytest.raises(ValueError, match="路径逃逸"):
            ctx.resolve("../etc/passwd")

    def test_absolute_path_outside_root(self, temp_workspace):
        ctx = ToolContext(root=str(temp_workspace))
        with pytest.raises(ValueError, match="路径逃逸"):
            ctx.resolve("/etc/passwd")

    def test_normal_path_allowed(self, temp_workspace):
        ctx = ToolContext(root=str(temp_workspace))
        resolved = ctx.resolve("README.md")
        assert resolved.is_file()

    def test_subdirectory_allowed(self, temp_workspace):
        ctx = ToolContext(root=str(temp_workspace))
        (temp_workspace / "src").mkdir(exist_ok=True)
        resolved = ctx.resolve("src")
        assert resolved.is_dir()
