"""write_file + patch_file 单测。"""

from unittest.mock import patch

import pytest

from agent_runtime.atomic_io import atomic_write_text
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import tool_patch_file, tool_write_file


@pytest.fixture
def ctx(temp_workspace):
    """基于临时 git 仓库的 ToolContext。"""
    return ToolContext(root=str(temp_workspace))


class TestWriteFile:
    """write_file 工具测试。"""

    def test_normal_write(self, ctx, temp_workspace):
        result = tool_write_file(ctx, {"path": "hello.txt", "content": "hello world"})
        assert "已写入" in result
        assert (temp_workspace / "hello.txt").read_text() == "hello world"

    def test_overwrite_existing(self, ctx, temp_workspace):
        (temp_workspace / "exist.txt").write_text("old")
        result = tool_write_file(ctx, {"path": "exist.txt", "content": "new"})
        assert "已写入" in result
        assert (temp_workspace / "exist.txt").read_text() == "new"

    def test_creates_parent_directories(self, ctx, temp_workspace):
        result = tool_write_file(ctx, {"path": "sub/deep/nested/file.py", "content": "x=1"})
        assert "已写入" in result
        assert (temp_workspace / "sub" / "deep" / "nested" / "file.py").read_text() == "x=1"

    def test_missing_path(self, ctx):
        result = tool_write_file(ctx, {"content": "no path"})
        assert "Error" in result

    def test_append_to_existing(self, ctx, temp_workspace):
        (temp_workspace / "log.txt").write_text("line1\n")
        result = tool_write_file(ctx, {"path": "log.txt", "content": "line2\n", "append": True})
        assert "已追加到" in result
        assert (temp_workspace / "log.txt").read_text() == "line1\nline2\n"

    def test_append_to_new_file(self, ctx, temp_workspace):
        """append 到不存在的文件 → 创建新文件。"""
        result = tool_write_file(ctx, {"path": "new.txt", "content": "hello", "append": True})
        assert "已写入" in result
        assert (temp_workspace / "new.txt").read_text() == "hello"

    def test_atomic_write_no_tmp_residue(self, ctx, temp_workspace):
        result = tool_write_file(ctx, {"path": "clean.txt", "content": "ok"})
        assert "已写入" in result
        assert (temp_workspace / "clean.txt").read_text() == "ok"
        assert not (temp_workspace / "clean.txt.tmp").exists()

    def test_atomic_write_preserves_original_on_failure(self, ctx, temp_workspace):
        target = temp_workspace / "keep.txt"
        target.write_text("original")
        with patch.object(type(target), "replace", side_effect=OSError("replace failed")):
            result = tool_write_file(ctx, {"path": "keep.txt", "content": "broken"})
        assert "Error" in result
        assert target.read_text() == "original"
        assert not (temp_workspace / "keep.txt.tmp").exists()


class TestAtomicWriteText:
    """atomic_write_text 单元测试。"""

    def test_writes_new_file(self, tmp_path):
        path = tmp_path / "a.txt"
        atomic_write_text(path, "hello")
        assert path.read_text() == "hello"
        assert not path.with_suffix(path.suffix + ".tmp").exists()

    def test_overwrites_existing(self, tmp_path):
        path = tmp_path / "b.py"
        path.write_text("old")
        atomic_write_text(path, "new")
        assert path.read_text() == "new"


class TestPatchFile:
    """patch_file 工具测试。"""

    def test_patch_exact_one_match(self, ctx, temp_workspace):
        (temp_workspace / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        result = tool_patch_file(
            ctx,
            {
                "path": "calc.py",
                "old_text": "return a + b",
                "new_text": "return int(a) + int(b)",
            },
        )
        assert "已修补" in result
        assert "int(a) + int(b)" in (temp_workspace / "calc.py").read_text()

    def test_patch_zero_matches(self, ctx, temp_workspace):
        (temp_workspace / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        result = tool_patch_file(
            ctx,
            {
                "path": "calc.py",
                "old_text": "nonexistent code",
                "new_text": "x",
            },
        )
        assert "出现 0 次" in result

    def test_patch_multiple_matches(self, ctx, temp_workspace):
        (temp_workspace / "dup.py").write_text("x = 1\nx = 1\nx = 1\n")
        result = tool_patch_file(
            ctx,
            {
                "path": "dup.py",
                "old_text": "x = 1",
                "new_text": "y = 2",
            },
        )
        assert "出现 3 次" in result

    def test_patch_nonexistent_file(self, ctx):
        result = tool_patch_file(
            ctx,
            {
                "path": "ghost.py",
                "old_text": "a",
                "new_text": "b",
            },
        )
        assert "Error" in result
