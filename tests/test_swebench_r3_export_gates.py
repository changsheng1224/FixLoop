"""R3 导出闸门单测（E12/E13/E14/E15）— 合成夹具，无 instance 特判。"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from src.benchmark.swebench.patch_export import (
    MAX_EXPORT_FILES,
    collect_repo_diff_safe,
    export_model_patch,
    gate_export_size,
    lines_to_unified_diff,
    looks_like_unified_diff,
    patch_from_state,
)
from src.state import CandidatePatch, RepairState


class TestE13UnifiedFromLines:
    def test_builds_headers_from_original_patched(self):
        u = lines_to_unified_diff("pkg/a.py", "x = 1\n", "x = 2\n")
        assert looks_like_unified_diff(u)
        assert "--- a/pkg/a.py" in u
        assert "+++ b/pkg/a.py" in u
        assert "-x = 1" in u
        assert "+x = 2" in u

    def test_fragment_alone_not_unified(self):
        assert looks_like_unified_diff("-old\n+new\n") is False

    def test_patch_from_state_synthesizes_headers(self):
        state = RepairState(
            issue_input="x",
            candidate_patches=[
                CandidatePatch(
                    file_path="lib/mod.py",
                    original_lines="a\n",
                    patched_lines="b\n",
                )
            ],
        )
        out = patch_from_state(state)
        assert looks_like_unified_diff(out)
        assert "lib/mod.py" in out


class TestE12DirtyExportGate:
    def test_no_candidates_skips_full_tree_diff(self, tmp_path):
        original = tmp_path / "orig"
        modified = tmp_path / "mod"
        original.mkdir()
        modified.mkdir()
        (original / "a.py").write_text("1\n", encoding="utf-8")
        (modified / "a.py").write_text("2\n", encoding="utf-8")
        # 再造大量无关变更
        for i in range(40):
            (original / f"noise_{i}.py").write_text("x\n", encoding="utf-8")
            (modified / f"noise_{i}.py").write_text("y\n", encoding="utf-8")

        state = RepairState(issue_input="x", candidate_patches=[])
        out = export_model_patch(
            state=state, original_repo=original, modified_repo=modified
        )
        assert out == ""

    def test_scoped_diff_only_candidate_paths(self, tmp_path):
        original = tmp_path / "orig"
        modified = tmp_path / "mod"
        original.mkdir()
        modified.mkdir()
        (original / "keep.py").write_text("old\n", encoding="utf-8")
        (modified / "keep.py").write_text("new\n", encoding="utf-8")
        (original / "noise.py").write_text("a\n", encoding="utf-8")
        (modified / "noise.py").write_text("b\n", encoding="utf-8")

        state = RepairState(
            issue_input="x",
            candidate_patches=[
                CandidatePatch(file_path="keep.py", original_lines="", patched_lines="")
            ],
        )
        # cand 无 lines/diff → 走 scoped repo diff
        out = export_model_patch(
            state=state, original_repo=original, modified_repo=modified
        )
        assert "keep.py" in out
        assert "noise.py" not in out

    def test_too_many_files_returns_empty(self, tmp_path):
        original = tmp_path / "orig"
        modified = tmp_path / "mod"
        original.mkdir()
        modified.mkdir()
        n = MAX_EXPORT_FILES + 5
        for i in range(n):
            (original / f"f{i}.py").write_text("a\n", encoding="utf-8")
            (modified / f"f{i}.py").write_text("b\n", encoding="utf-8")
        assert collect_repo_diff_safe(original, modified) == ""

    def test_gate_rejects_non_unified(self):
        assert gate_export_size("-a\n+b\n") == ""


class TestE6aExportWithoutApply:
    def test_apply_failed_state_exports_empty_without_dirty_fallback(self, tmp_path):
        original = tmp_path / "o"
        modified = tmp_path / "m"
        original.mkdir()
        modified.mkdir()
        (original / "x.py").write_text("1\n", encoding="utf-8")
        (modified / "x.py").write_text("2\n", encoding="utf-8")
        state = RepairState(
            issue_input="x",
            candidate_patches=[],
            agent_errors={"patcher_apply": "hunk_mismatch:x.py"},
            node_timings={"patcher_apply_failed": True},
        )
        assert (
            export_model_patch(state=state, original_repo=original, modified_repo=modified)
            == ""
        )
