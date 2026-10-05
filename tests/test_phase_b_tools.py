"""Phase B：quick_test / write_serial / read window。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import ReadFileArgs, tool_expand_lock, tool_quick_test, tool_write_file
from src.repair.execution.edit_lock import EditLockState


def test_read_file_default_window_is_100():
    assert ReadFileArgs.__dataclass_fields__["end"].default == 100


def test_write_serial_blocks_second_write():
    raw = tempfile.mkdtemp(prefix="fixloop-ws-")
    root = Path(raw)
    (root / "a.py").write_text("x\n", encoding="utf-8")
    (root / "b.py").write_text("y\n", encoding="utf-8")
    ctx = ToolContext(root=str(root))
    lock = EditLockState(repo_root=root, allowed_edit={"a.py", "b.py"})
    lock.mark_read("a.py")
    lock.mark_read("b.py")
    lock.write_serial = True
    ctx.edit_lock = lock
    try:
        lock.begin_turn()
        r1 = tool_write_file(ctx, {"path": "a.py", "content": "1\n"}).content
        assert "Error" not in r1 or "已写入" in r1
        r2 = tool_write_file(ctx, {"path": "b.py", "content": "2\n"}).content
        assert r2.startswith("Error: write_serial")
        lock.begin_turn()
        r3 = tool_write_file(ctx, {"path": "b.py", "content": "2\n"}).content
        assert "已写入" in r3
    finally:
        ctx.edit_lock = None


def test_expand_lock_tool():
    raw = tempfile.mkdtemp(prefix="fixloop-el-")
    root = Path(raw)
    (root / "a.py").write_text("x\n", encoding="utf-8")
    (root / "b.py").write_text("y\n", encoding="utf-8")
    ctx = ToolContext(root=str(root))
    lock = EditLockState(repo_root=root, allowed_edit={"a.py"})
    ctx.edit_lock = lock
    try:
        out = tool_expand_lock(ctx, {"path": "b.py"}).content
        assert "ok" in out.lower()
        assert "b.py" in lock.allowed_edit
    finally:
        ctx.edit_lock = None


def test_quick_test_missing_target():
    ctx = ToolContext(root=".")
    out = tool_quick_test(ctx, {}).content
    assert out.startswith("Error")
