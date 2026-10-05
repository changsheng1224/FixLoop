"""apply_patch 格式解析与 ACI 行为。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from agent_runtime.apply_patch_format import parse_apply_patch_text, strip_fences
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import _normalize_hunk_headers, tool_apply_patch
from src.repair.execution.edit_lock import EditLockState

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
    ctx.edit_lock = lock
    try:
        out = tool_apply_patch(ctx, {"patch": SAMPLE}).content
        assert "ok" in out.lower() or "已修补" in out or "apply_patch" in out.lower()
        assert "new" in (root / "a.py").read_text(encoding="utf-8")
        assert "写后窗口" in out or "after:" in out.lower() or "|" in out
        assert lock.apply_patch_ok_count >= 1
    finally:
        ctx.edit_lock = None


def test_bare_multi_hunk_headers_are_located_independently():
    text = "first = 1\nmiddle = 2\nlast = 3\n"
    diff = """@@
-first = 1
+first = 10
@@
-last = 3
+last = 30"""
    normalized = _normalize_hunk_headers(diff, text)
    assert "@@ -1,1 +1,1 @@" in normalized
    assert "@@ -3,1 +3,1 @@" in normalized


def test_bare_multi_hunk_headers_advance_past_duplicate_preimage():
    text = "value = 1\nother = 2\nvalue = 1\n"
    diff = """@@
-value = 1
+value = 10
@@
-value = 1
+value = 11"""
    normalized = _normalize_hunk_headers(diff, text)
    assert "@@ -1,1 +1,1 @@" in normalized
    assert "@@ -3,1 +3,1 @@" in normalized


def test_tool_applies_duplicate_preimage_hunks_with_context():
    raw = tempfile.mkdtemp(prefix="fixloop-ap-dupe-")
    root = Path(raw)
    (root / "a.py").write_text("header = 0\nvalue = 1\nmiddle = 2\nvalue = 1\n", encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: a.py
@@
 header = 0
-value = 1
+value = 10
@@
 middle = 2
-value = 1
+value = 11
*** End Patch"""
    ctx = ToolContext(root=str(root))
    lock = EditLockState(repo_root=root, allowed_edit={"a.py"})
    lock.mark_read("a.py")
    ctx.edit_lock = lock
    try:
        out = tool_apply_patch(ctx, {"patch": patch}).content
        assert out.startswith("ok apply_patch")
        assert (root / "a.py").read_text(encoding="utf-8") == (
            "header = 0\nvalue = 10\nmiddle = 2\nvalue = 11\n"
        )
    finally:
        ctx.edit_lock = None


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
    ctx.edit_lock = lock
    try:
        out = tool_apply_patch(ctx, {"patch": bad}).content
        assert out.startswith("Error")
        assert "lint" in out.lower() or "syntax" in out.lower()
        assert (root / "a.py").read_text(encoding="utf-8") == "x = 1\n"
        assert lock.edit_lint_reject_count >= 1
    finally:
        ctx.edit_lock = None


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
    ctx.edit_lock = lock
    try:
        out = tool_apply_patch(ctx, {"patch": bad}).content
        assert out.startswith("Error")
        assert "preimage" in out.lower() or "empty_original" in out.lower() or "上下文" in out
    finally:
        ctx.edit_lock = None


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
    ctx.edit_lock = lock
    try:
        out = tool_apply_patch(ctx, {"patch": stale}).content
        assert out.startswith("Error")
        assert "near=" in out.lower() or "未匹配" in out or "stale" in out.lower()
    finally:
        ctx.edit_lock = None
