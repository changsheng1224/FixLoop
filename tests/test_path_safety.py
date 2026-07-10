"""path_safety 符号链接逃逸检测单测。"""

import os
import sys

import pytest

from agent_runtime.path_safety import is_path_under_root, resolve_under_root
from agent_runtime.tool_context import ToolContext


def _symlink_or_skip(src, dst, *, target_is_directory=False):
    try:
        os.symlink(src, dst, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks in this environment")


class TestIsPathUnderRoot:
    def test_child_is_under(self, temp_workspace):
        root = temp_workspace.resolve()
        assert is_path_under_root(root / "README.md", root)

    def test_parent_not_under(self, temp_workspace):
        root = temp_workspace.resolve()
        assert not is_path_under_root(temp_workspace.parent, root)


class TestResolveUnderRoot:
    def test_normal_file(self, temp_workspace):
        resolved = resolve_under_root(temp_workspace, "README.md")
        assert resolved.is_file()

    def test_parent_escape_rejected(self, temp_workspace):
        with pytest.raises(ValueError, match="路径逃逸"):
            resolve_under_root(temp_workspace, "../etc/passwd")

    def test_absolute_outside_rejected(self, temp_workspace):
        with pytest.raises(ValueError, match="路径逃逸"):
            resolve_under_root(temp_workspace, "/etc/passwd")

    def test_symlink_to_outside_file_rejected(self, temp_workspace):
        outside = temp_workspace.parent / "bonus11_outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = temp_workspace / "evil_link"
        _symlink_or_skip(outside, link)
        with pytest.raises(ValueError, match="符号链接逃逸"):
            resolve_under_root(temp_workspace, "evil_link")

    def test_symlink_to_internal_file_allowed(self, temp_workspace):
        inner = temp_workspace / "inner.txt"
        inner.write_text("ok", encoding="utf-8")
        link = temp_workspace / "good_link"
        _symlink_or_skip("inner.txt", link)
        resolved = resolve_under_root(temp_workspace, "good_link")
        assert resolved == inner.resolve()

    def test_symlink_chain_escape_rejected(self, temp_workspace):
        sub = temp_workspace / "sub"
        sub.mkdir()
        outside = temp_workspace.parent / "bonus11_outside_dir"
        outside.mkdir(exist_ok=True)
        link = sub / "escape"
        _symlink_or_skip("../../bonus11_outside_dir", link, target_is_directory=True)
        with pytest.raises(ValueError, match="符号链接逃逸"):
            resolve_under_root(temp_workspace, "sub/escape")


class TestToolContextUsesPathSafety:
    def test_resolve_delegates_to_path_safety(self, temp_workspace):
        ctx = ToolContext(root=str(temp_workspace))
        outside = temp_workspace.parent / "bonus11_ctx_outside.txt"
        outside.write_text("x", encoding="utf-8")
        link = temp_workspace / "ctx_evil"
        _symlink_or_skip(outside, link)
        with pytest.raises(ValueError, match="符号链接逃逸"):
            ctx.resolve("ctx_evil")

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows drive-letter escape")
    def test_cross_drive_rejected_on_windows(self, temp_workspace):
        """跨盘符路径在 Windows 上应拒绝（若存在 D:）。"""
        if not os.path.exists("D:\\"):
            pytest.skip("no D: drive")
        with pytest.raises(ValueError, match="路径逃逸|无法解析"):
            resolve_under_root(temp_workspace, "D:\\outside.txt")
