"""改无关文件检测 + faithfulness 闸口单测（V1.4-Bonus6a）。"""

from __future__ import annotations

from src.repair.failure_tags import (
    FailureTag,
    _is_wrong_file,
    allowed_patch_files,
    check_patch_faithfulness,
    classify_failure_tags,
)
from src.state import CandidatePatch, RepairState, SuspectLocation


# ---------------------------------------------------------------------------
# _is_wrong_file — 修复后的语义
# ---------------------------------------------------------------------------


class TestIsWrongFile:
    def test_all_patches_in_allowed(self):
        state = RepairState(issue_input="test")
        state.suspect_locations = [SuspectLocation(file_path="app.py", start_line=1, end_line=10)]
        state.candidate_patches = [
            CandidatePatch(file_path="app.py", diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@"),
        ]
        assert not _is_wrong_file(state)

    def test_one_patch_outside_allowed(self):
        """任一 patch 文件不在 allowed → wrong_file=True。"""
        state = RepairState(issue_input="test")
        state.suspect_locations = [SuspectLocation(file_path="app.py", start_line=1, end_line=10)]
        state.candidate_patches = [
            CandidatePatch(file_path="app.py", diff="..."),
            CandidatePatch(file_path="unrelated.py", diff="..."),  # ← 幻觉
        ]
        assert _is_wrong_file(state)

    def test_all_patches_outside_allowed(self):
        state = RepairState(issue_input="test")
        state.suspect_locations = [SuspectLocation(file_path="app.py", start_line=1, end_line=10)]
        state.candidate_patches = [
            CandidatePatch(file_path="unrelated.py", diff="..."),
        ]
        assert _is_wrong_file(state)

    def test_no_allowed_no_violation(self):
        """无 suspect → 不判定为 wrong_file（信息不足）。"""
        state = RepairState(issue_input="test")
        state.candidate_patches = [
            CandidatePatch(file_path="x.py", diff="..."),
        ]
        assert not _is_wrong_file(state)

    def test_empty_patches(self):
        state = RepairState(issue_input="test")
        assert not _is_wrong_file(state)


# ---------------------------------------------------------------------------
# check_patch_faithfulness — 闸口
# ---------------------------------------------------------------------------


class TestCheckFaithfulness:
    def test_all_patches_pass(self):
        state = RepairState(issue_input="test")
        state.suspect_locations = [
            SuspectLocation(file_path="app.py", start_line=1, end_line=10),
        ]
        patches = [
            CandidatePatch(file_path="app.py", diff="..."),
        ]
        kept, rejected = check_patch_faithfulness(patches, state)
        assert len(kept) == 1
        assert len(rejected) == 0

    def test_hallucinated_patch_rejected(self):
        state = RepairState(issue_input="test")
        state.suspect_locations = [
            SuspectLocation(file_path="app.py", start_line=1, end_line=10),
        ]
        patches = [
            CandidatePatch(file_path="app.py", diff="..."),
            CandidatePatch(file_path="evil.py", diff="..."),
        ]
        kept, rejected = check_patch_faithfulness(patches, state)
        assert len(kept) == 1
        assert kept[0].file_path == "app.py"
        assert rejected == ["evil.py"]

    def test_all_hallucinated_all_rejected(self):
        state = RepairState(issue_input="test")
        state.suspect_locations = [
            SuspectLocation(file_path="app.py", start_line=1, end_line=10),
        ]
        patches = [
            CandidatePatch(file_path="evil.py", diff="..."),
        ]
        kept, rejected = check_patch_faithfulness(patches, state)
        assert len(kept) == 0
        assert rejected == ["evil.py"]

    def test_empty_allowed_passes_all(self):
        state = RepairState(issue_input="test")
        patches = [
            CandidatePatch(file_path="x.py", diff="..."),
        ]
        kept, rejected = check_patch_faithfulness(patches, state)
        assert len(kept) == 1
        assert len(rejected) == 0


# ---------------------------------------------------------------------------
# classify_failure_tags — WRONG_FILE
# ---------------------------------------------------------------------------


class TestClassifyWrongFile:
    def test_wrong_file_tagged(self):
        state = RepairState(issue_input="test")
        state.suspect_locations = [
            SuspectLocation(file_path="app.py", start_line=1, end_line=10),
        ]
        state.candidate_patches = [
            CandidatePatch(file_path="unrelated.py", diff="..."),
        ]
        tags = classify_failure_tags(state)
        assert FailureTag.WRONG_FILE in tags

    def test_correct_file_not_wrong(self):
        state = RepairState(issue_input="test")
        state.suspect_locations = [
            SuspectLocation(file_path="app.py", start_line=1, end_line=10),
        ]
        state.candidate_patches = [
            CandidatePatch(file_path="app.py", diff="..."),
        ]
        state.status = "fixed"  # simulate success
        tags = classify_failure_tags(state)
        assert FailureTag.WRONG_FILE not in tags


# ---------------------------------------------------------------------------
# allowed_patch_files
# ---------------------------------------------------------------------------


class TestAllowedPatchFiles:
    def test_includes_suspect_locations(self):
        state = RepairState(issue_input="test")
        state.suspect_locations = [
            SuspectLocation(file_path="src/app.py", start_line=1, end_line=10),
        ]
        allowed = allowed_patch_files(state)
        assert "src/app.py" in allowed

    def test_normalizes_paths(self):
        state = RepairState(issue_input="test")
        state.suspect_locations = [
            SuspectLocation(file_path="./src/app.py", start_line=1, end_line=10),
        ]
        allowed = allowed_patch_files(state)
        assert "src/app.py" in allowed
        assert "./src/app.py" not in allowed
