"""Critic rules_first：提交 Verifier 前廉价过滤（空 / 越锁 / 仅测）。"""

from __future__ import annotations

from src.repair.critic import CriticVerdict, review_patch
from src.state import CandidatePatch


def test_empty_patch_rejected():
    v = review_patch([], allowed_edit={"a.py"}, mode="rules_first")
    assert v.accepted is False
    assert "empty" in v.reason.lower() or "空" in v.reason


def test_out_of_lock_not_critic_gated():
    """越锁由 edit_lock 写时拦截；critic 不再双闸。"""
    patches = [
        CandidatePatch(
            file_path="evil.py",
            original_lines="a",
            patched_lines="b",
            diff="--- a/evil.py\n+++ b/evil.py\n@@ -1 +1 @@\n-a\n+b\n",
        )
    ]
    v = review_patch(patches, allowed_edit={"ok.py"}, mode="rules_first")
    assert v.accepted is True


def test_valid_impl_diff_accepted():
    patches = [
        CandidatePatch(
            file_path="pkg/mod.py",
            original_lines="x",
            patched_lines="y",
            diff="--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1 +1 @@\n-x\n+y\n",
        )
    ]
    v = review_patch(patches, allowed_edit={"pkg/mod.py"}, mode="rules_first")
    assert v.accepted is True


def test_tests_only_rejected():
    patches = [
        CandidatePatch(
            file_path="tests/test_foo.py",
            original_lines="a",
            patched_lines="b",
            diff="--- a/tests/test_foo.py\n+++ b/tests/test_foo.py\n@@ -1 +1 @@\n-a\n+b\n",
        )
    ]
    v = review_patch(
        patches,
        allowed_edit={"tests/test_foo.py", "pkg/mod.py"},
        mode="rules_first",
        reject_tests_only=True,
    )
    assert v.accepted is False


def test_mode_off_always_accepts():
    v = review_patch([], allowed_edit=set(), mode="off")
    assert v.accepted is True
    assert v.mode == "off"


def test_verdict_fields():
    v = CriticVerdict(accepted=True, reason="ok", mode="rules_first")
    assert v.accepted and v.reason == "ok"


def test_adjacent_duplicate_addition_rejected():
    patches = [
        CandidatePatch(
            file_path="pkg/mod.py",
            original_lines="a",
            patched_lines="b",
            diff=(
                "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1,3 +1,4 @@\n"
                " a\n+sys.exit(1)\n+sys.exit(1)\n b\n"
            ),
        )
    ]
    v = review_patch(patches, allowed_edit={"pkg/mod.py"}, mode="rules_first")
    assert v.accepted is False
    assert v.reason == "redundant_duplicate_addition"


def test_existing_duplicate_not_rejected_when_patch_changes_another_line():
    patches = [
        CandidatePatch(
            file_path="pkg/mod.py",
            original_lines="value = 1\nvalue = 1\nresult = 2",
            patched_lines="value = 1\nvalue = 1\nresult = 3",
            diff=(
                "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1,3 +1,3 @@\n"
                " value = 1\n value = 1\n-result = 2\n+result = 3\n"
            ),
        )
    ]
    assert review_patch(patches, mode="rules_first").accepted is True
