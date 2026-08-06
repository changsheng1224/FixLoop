"""工具层 edit_lock：未读不可写。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import tool_patch_file, tool_read_file, tool_write_file
from src.repair.execution.edit_lock import EditLockState, set_active_edit_lock


def test_tool_write_rejected_until_read():
    raw = tempfile.mkdtemp(prefix="fixloop-tool-elock-")
    root = Path(raw)
    (root / "a.py").write_text("x\n", encoding="utf-8")
    ctx = ToolContext(root=str(root))
    lock = EditLockState(repo_root=root, allowed_edit={"a.py"})
    set_active_edit_lock(root, lock)
    try:
        out = tool_write_file(ctx, {"path": "a.py", "content": "y\n"})
        assert out.startswith("Error: edit_lock")
        assert "unread" in out
        read_out = tool_read_file(ctx, {"path": "a.py"})
        assert not read_out.startswith("Error")
        out2 = tool_write_file(ctx, {"path": "a.py", "content": "y\n"})
        assert out2.startswith("已写入") or "写入" in out2
    finally:
        set_active_edit_lock(root, None)


def test_tool_patch_respects_allowed_edit():
    raw = tempfile.mkdtemp(prefix="fixloop-tool-elock2-")
    root = Path(raw)
    (root / "a.py").write_text("old\n", encoding="utf-8")
    (root / "b.py").write_text("keep\n", encoding="utf-8")
    ctx = ToolContext(root=str(root))
    lock = EditLockState(repo_root=root, allowed_edit={"a.py"})
    lock.mark_read("a.py")
    lock.mark_read("b.py")
    set_active_edit_lock(root, lock)
    try:
        bad = tool_patch_file(
            ctx, {"path": "b.py", "old_text": "keep\n", "new_text": "hack\n"}
        )
        assert bad.startswith("Error: edit_lock")
        good = tool_patch_file(
            ctx, {"path": "a.py", "old_text": "old\n", "new_text": "new\n"}
        )
        assert "已修补" in good
    finally:
        set_active_edit_lock(root, None)
