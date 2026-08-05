"""SWE Lite 飞轮通用修复单测（合成夹具，无 instance 特判）。"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from agent_runtime.intent.stack_parse import (
    parse_stack,
    relativize_suspect_path,
)
from agent_runtime.step_guard import StepContext, StepGuard
from src.benchmark.swebench.patch_export import normalize_patch_lf
from src.repair.failure_tags import FailureTag, classify_failure_tags
from src.repair.patch_applier import PatchApplier, sibling_pattern_remains
from src.repair.pipeline import _split_grep_path_line
from src.repair.termination import RepairTerminalStatus
from src.skills.skill_block import render_skill_hint_for_plan
from src.state import CandidatePatch, RepairPlan, RepairState, SkillContext


class TestE1NormalizePatchLf:
    def test_strips_crlf_and_bare_cr(self):
        raw = "--- a/x.py\r\n+++ b/x.py\r\n@@ -1 +1 @@\r\n-old\r\n+new\r\n"
        out = normalize_patch_lf(raw)
        assert "\r" not in out
        assert out.count("\n") >= 4
        assert normalize_patch_lf("a\rb\n") == "ab\n"


class TestE2RelativizeSuspectPath:
    def test_drops_foreign_abs_without_pkg_suffix(self):
        assert relativize_suspect_path("/Users/mark/venv310/bin/pylint") is None

    def test_keeps_trailing_pkg_path(self):
        p = relativize_suspect_path(
            "C:/temp/matplotlib_save_ps/venv/lib/site-packages/matplotlib/backends/backend_ps.py"
        )
        # site-packages is noise → None
        assert p is None

    def test_maps_via_repo_root(self):
        root = Path(tempfile.mkdtemp(prefix="relpath-"))
        try:
            (root / "pkg").mkdir()
            f = root / "pkg" / "mod.py"
            f.write_text("x\n", encoding="utf-8")
            got = relativize_suspect_path(
                "/Users/other/project/pkg/mod.py",
                repo_root=root,
            )
            assert got == "pkg/mod.py"
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_drops_foreign_relative_when_repo_root_set(self):
        """E2′: temp/repro.py must not survive as suspect when absent from repo."""
        root = Path(tempfile.mkdtemp(prefix="relpath-e2b-"))
        try:
            (root / "lib").mkdir()
            (root / "lib" / "ok.py").write_text("pass\n", encoding="utf-8")
            assert (
                relativize_suspect_path(
                    "C:/temp/repro_case/save_ps.py",
                    repo_root=root,
                )
                is None
            )
            assert (
                relativize_suspect_path(
                    "temp/repro_case/save_ps.py",
                    repo_root=root,
                )
                is None
            )
            # still maps when suffix exists in repo (avoid /venv/ noise markers)
            (root / "lib" / "backends").mkdir()
            target = root / "lib" / "backends" / "backend_ps.py"
            target.write_text("x\n", encoding="utf-8")
            got = relativize_suspect_path(
                "C:/temp/repro/lib/backends/backend_ps.py",
                repo_root=root,
            )
            assert got == "lib/backends/backend_ps.py"
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_parse_stack_drops_users_abs(self):
        text = '''
Traceback (most recent call last):
  File "/Users/markbyrne/programming/pylint/pylint/lint/run.py", line 10, in main
    f()
  File "src/app.py", line 1, in f
    pass
TypeError: boom
'''
        parsed = parse_stack(text)
        assert all(not s.startswith("/Users/") for s in parsed.suspect_files)
        for s in parsed.suspect_files:
            assert not s.startswith("C:")


class TestE10GoalDriftInRepoAnchor:
    def test_no_drift_when_suspects_empty_after_foreign_drop(self):
        """仓外锚点清空后，读库内文件不得 goal_drift。"""
        guard = StepGuard(stall_threshold=10, drift_terminate=3)
        guard.reset(task_summary="fix save_ps.py", suspect_files=set())
        for _ in range(4):
            v = guard.evaluate(
                StepContext(
                    tool_name="read_file",
                    tool_args={"path": "lib/ok.py"},
                    has_affected=True,
                )
            )
            assert v is None


class TestE8GrepPathSplit:
    def test_windows_drive_not_split_at_first_colon(self):
        line = r"C:\Users\me\repo\pkg\a.py:42:def foo():"
        hit = _split_grep_path_line(line)
        assert hit is not None
        fpath, lineno, text = hit
        assert fpath == r"C:\Users\me\repo\pkg\a.py"
        assert lineno == "42"
        assert "def foo" in text

    def test_unix_relative(self):
        hit = _split_grep_path_line("pkg/a.py:3:x = 1")
        assert hit == ("pkg/a.py", "3", "x = 1")


class TestE3SkillAclFilter:
    def test_retriever_drops_stack_parse_from_tool_chain(self):
        plan = RepairPlan(
            issue_type="type_error",
            skill=SkillContext(
                matched_skill="python_type_error_fix",
                suggested_tools=["stack_parse", "ast_parse", "search", "patch_file", "read_file"],
                guidance=["g1"],
            ),
        )
        render = render_skill_hint_for_plan(plan, "retriever")
        assert "工具序:" in render.text
        assert "stack_parse" not in render.text
        assert "ast_parse" not in render.text
        assert "patch_file" not in render.text
        assert "search" in render.text
        assert "read_file" in render.text

    def test_localizer_keeps_stack_parse(self):
        plan = RepairPlan(
            issue_type="type_error",
            skill=SkillContext(
                matched_skill="python_type_error_fix",
                suggested_tools=["stack_parse", "search"],
            ),
        )
        render = render_skill_hint_for_plan(plan, "localizer")
        assert "stack_parse" in render.text


class TestE6aApplyFailedTag:
    def test_apply_failed_tag_from_agent_errors(self):
        state = RepairState(
            issue_input="x",
            status=RepairTerminalStatus.EXHAUSTED,
            retry_count=2,
            agent_errors={"patcher_apply": "hunk_mismatch:pkg/a.py"},
            node_timings={"patcher_apply_failed": True},
        )
        assert classify_failure_tags(state) == [FailureTag.APPLY_FAILED]

    def test_applier_records_path_reject_reason(self):
        root = Path(tempfile.mkdtemp(prefix="apply-e6a-"))
        try:
            (root / "ok.py").write_text("x = 1\n", encoding="utf-8")
            applier = PatchApplier(str(root))
            applied = applier.apply_patches(
                [
                    CandidatePatch(
                        file_path="temp/missing.py",
                        original_lines="a",
                        patched_lines="b",
                    )
                ]
            )
            assert applied == []
            assert any("path_not_in_repo" in e for e in applier.last_apply_errors)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestE7SiblingPattern:
    def test_detects_remaining_original(self):
        patch = CandidatePatch(
            file_path="a.py",
            original_lines="regex = r'x$'",
            patched_lines="regex = r'x\\Z'",
        )
        after = "regex = r'x\\Z'\nregex = r'x$'\n"
        assert sibling_pattern_remains(after, patch) is True
        assert sibling_pattern_remains("regex = r'x\\Z'\n", patch) is False
