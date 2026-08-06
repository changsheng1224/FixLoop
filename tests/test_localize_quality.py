"""定位质量：栈接地、排序、关键词去噪。"""

from __future__ import annotations

from pathlib import Path

from src.repair.localization.issue_paths import extract_paths_from_issue
from src.repair.localization.localize_quality import (
    normalize_repo_path,
    refine_suspects,
    retrieve_keywords,
    score_suspect,
    suspects_from_issue,
)
from src.state import SuspectLocation


def _repo_with_bug(tmp: Path) -> Path:
    src = tmp / "pkg" / "core.py"
    src.parent.mkdir(parents=True)
    src.write_text("def boom():\n    raise ValueError('x')\n", encoding="utf-8")
    test = tmp / "tests" / "test_core.py"
    test.parent.mkdir(parents=True)
    test.write_text("def test_boom():\n    boom()\n", encoding="utf-8")
    return tmp


class TestIssuePaths:
    def test_extracts_relative_and_file_forms(self):
        issue = (
            'File "pkg/core.py", line 2, in boom\n'
            "ValueError\n"
            "see also pkg/core.py:2 and `pkg/core.py`"
        )
        paths = extract_paths_from_issue(issue)
        assert "pkg/core.py" in paths


class TestSuspectsFromIssue:
    def test_stack_prefers_impl_over_test(self, tmp_path: Path):
        root = _repo_with_bug(tmp_path)
        issue = (
            'Traceback (most recent call last):\n'
            f'  File "{root / "tests" / "test_core.py"}", line 2, in test_boom\n'
            "    boom()\n"
            f'  File "{root / "pkg" / "core.py"}", line 2, in boom\n'
            "    raise ValueError('x')\n"
            "ValueError: x\n"
        )
        suspects = suspects_from_issue(issue, root)
        assert suspects
        assert suspects[0].file_path == "pkg/core.py"
        assert suspects[0].function_name == "boom"
        assert suspects[0].start_line == 2


class TestRefineSuspects:
    def test_drops_missing_and_ranks_stack(self, tmp_path: Path):
        root = _repo_with_bug(tmp_path)
        issue = (
            'File "pkg/core.py", line 2, in boom\n'
            "ValueError: x\n"
        )
        noisy = [
            SuspectLocation(file_path="does/not/exist.py", start_line=1, end_line=1),
            SuspectLocation(
                file_path="tests/test_core.py",
                start_line=1,
                end_line=1,
                confidence=0.9,
            ),
        ]
        refined = refine_suspects(noisy, issue, root, plan=None, max_keep=5)
        assert refined
        assert refined[0].file_path == "pkg/core.py"
        assert all((root / s.file_path).is_file() for s in refined)

    def test_plan_files_boost(self, tmp_path: Path):
        root = _repo_with_bug(tmp_path)
        s = SuspectLocation(file_path="pkg/core.py", start_line=1, end_line=1, confidence=0.5)
        score = score_suspect(
            s,
            repo_root=root,
            issue_paths=set(),
            stack_files=set(),
            plan_files={"pkg/core.py"},
        )
        assert score > 0.5


class TestRetrieveKeywords:
    def test_uses_function_not_random_quotes(self):
        suspects = [
            SuspectLocation(
                file_path="a.py",
                start_line=1,
                end_line=1,
                function_name="compute_total",
            )
        ]
        issue = 'please fix "the" "bug" in compute_total and also "error"'
        kws = retrieve_keywords(suspects, issue)
        assert "compute_total" in kws
        assert "the" not in kws
        assert "bug" not in kws
        assert "error" not in kws


class TestNormalizePath:
    def test_relative_exists(self, tmp_path: Path):
        root = _repo_with_bug(tmp_path)
        assert normalize_repo_path("pkg/core.py", root) == "pkg/core.py"
        assert normalize_repo_path("/usr/lib/python/site-packages/x.py", root) is None
