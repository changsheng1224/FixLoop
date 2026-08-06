"""apply_patch 格式解析与 ACI 行为。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from agent_runtime.apply_patch_format import parse_apply_patch_text, strip_fences
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import tool_apply_patch
from src.repair.execution.edit_lock import EditLockState, set_active_edit_lock

SAMPLE = """\
*** Begin Patch
*** Update File: a.py
@@
-old
+new
*** End Patch
"""


def test_strip_fences():
    raw = "```\n*** Begin Patch\n*** End Patch\n```"
    assert "*** Begin Patch" in strip_fences(raw)


def test_parse_update_file():
    ops = parse_apply_patch_text(SAMPLE)
    assert len(ops) == 1
    assert ops[0].path == "a.py"
    assert ops[0].action == "update"
    assert "@@" in ops[0].diff


def test_tool_apply_patch_ok_with_echo_and_lint():
    raw = tempfile.mkdtemp(prefix="fixloop-ap-")
    root = Path(raw)
    (root / "a.py").write_text("old\n", encoding="utf-8")
    ctx = ToolContext(root=str(root))
    lock = EditLockState(repo_root=root, allowed_edit={"a.py"})
    lock.mark_read("a.py")
    set_active_edit_lock(root, lock)
    try:
        out = tool_apply_patch(ctx, {"patch": SAMPLE})
        assert "ok" in out.lower() or "已修补" in out or "apply_patch" in out.lower()
        assert "new" in (root / "a.py").read_text(encoding="utf-8")
        assert "写后窗口" in out or "after:" in out.lower() or "|" in out
        assert lock.apply_patch_ok_count >= 1
    finally:
        set_active_edit_lock(root, None)


def test_tool_apply_patch_lint_rejects_syntax():
    raw = tempfile.mkdtemp(prefix="fixloop-ap2-")
    root = Path(raw)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    bad = """\
*** Begin Patch
*** Update File: a.py
@@
-x = 1
+def (
*** End Patch
"""
    ctx = ToolContext(root=str(root))
    lock = EditLockState(repo_root=root, allowed_edit={"a.py"})
    lock.mark_read("a.py")
    set_active_edit_lock(root, lock)
    try:
        out = tool_apply_patch(ctx, {"patch": bad})
        assert out.startswith("Error")
        assert "lint" in out.lower() or "syntax" in out.lower()
        assert (root / "a.py").read_text(encoding="utf-8") == "x = 1\n"
        assert lock.edit_lint_reject_count >= 1
    finally:
        set_active_edit_lock(root, None)


def test_parse_rejects_empty_update_preimage():
    import pytest

    from agent_runtime.apply_patch_format import parse_apply_patch_text

    only_plus = """\
*** Begin Patch
*** Update File: a.py
@@
+new_only
*** End Patch
"""
    with pytest.raises(ValueError, match="preimage|empty"):
        parse_apply_patch_text(only_plus)

    empty_body = """\
*** Begin Patch
*** Update File: a.py
*** End Patch
"""
    with pytest.raises(ValueError, match="empty Update|preimage"):
        parse_apply_patch_text(empty_body)


def test_tool_rejects_empty_original_message():
    raw = tempfile.mkdtemp(prefix="fixloop-ap-empty-")
    root = Path(raw)
    (root / "a.py").write_text("old\n", encoding="utf-8")
    # Bypass parser by calling after a body that normalize might leave without -
    # Use parse-level rejection via tool input
    bad = """\
*** Begin Patch
*** Update File: a.py
@@
+only_add
*** End Patch
"""
    ctx = ToolContext(root=str(root))
    lock = EditLockState(repo_root=root, allowed_edit={"a.py"})
    lock.mark_read("a.py")
    set_active_edit_lock(root, lock)
    try:
        out = tool_apply_patch(ctx, {"patch": bad})
        assert out.startswith("Error")
        assert "preimage" in out.lower() or "empty_original" in out.lower() or "上下文" in out
    finally:
        set_active_edit_lock(root, None)


def test_stale_returns_near():
    raw = tempfile.mkdtemp(prefix="fixloop-ap3-")
    root = Path(raw)
    (root / "a.py").write_text("actual\n", encoding="utf-8")
    stale = """\
*** Begin Patch
*** Update File: a.py
@@
-missing
+new
*** End Patch
"""
    ctx = ToolContext(root=str(root))
    lock = EditLockState(repo_root=root, allowed_edit={"a.py"})
    lock.mark_read("a.py")
    set_active_edit_lock(root, lock)
    try:
        out = tool_apply_patch(ctx, {"patch": stale})
        assert out.startswith("Error")
        assert "near=" in out.lower() or "未匹配" in out or "stale" in out.lower()
    finally:
        set_active_edit_lock(root, None)
