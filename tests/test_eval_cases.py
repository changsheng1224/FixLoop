"""M7 评测 Case 库结构与健康检查。"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from src.eval.patch_utils import apply_unified_patch

CASES_DIR = Path(__file__).resolve().parents[1] / "src" / "eval" / "cases"
READY_CASES = [f"case_{i:03d}" for i in range(1, 11)]
SCAFFOLD_CASES: list[str] = []


def _case_dirs():
    return sorted(p for p in CASES_DIR.iterdir() if p.is_dir() and p.name.startswith("case_"))


class TestEvalCaseLayout:
    def test_ten_case_directories(self):
        names = {p.name for p in _case_dirs()}
        for i in range(1, 11):
            assert f"case_{i:03d}" in names

    @pytest.mark.parametrize("case_id", READY_CASES)
    def test_ready_case_has_required_files(self, case_id):
        d = CASES_DIR / case_id
        for name in (
            "issue.txt",
            "expected_patch.diff",
            "min_lines.txt",
            "metadata.yaml",
        ):
            assert (d / name).is_file(), f"{case_id} missing {name}"
        assert (d / "repo").is_dir()

    @pytest.mark.parametrize("case_id", READY_CASES)
    def test_metadata_verified(self, case_id):
        meta = (CASES_DIR / case_id / "metadata.yaml").read_text(encoding="utf-8")
        assert "status: verified" in meta


class TestEvalCaseHealth:
    @pytest.mark.parametrize("case_id", READY_CASES)
    def test_buggy_repo_pytest_fails(self, case_id):
        repo = CASES_DIR / case_id / "repo"
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0, f"{case_id} buggy repo should fail pytest"

    @pytest.mark.parametrize("case_id", READY_CASES)
    def test_expected_patch_fixes_repo(self, case_id):
        case_dir = CASES_DIR / case_id
        repo = case_dir / "repo"
        patch = (case_dir / "expected_patch.diff").read_text(encoding="utf-8")
        assert patch.strip() and not patch.startswith("# TODO")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_repo = Path(tmp) / "repo"
            shutil.copytree(repo, tmp_repo)
            apply_unified_patch(tmp_repo, patch)
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q"],
                cwd=tmp_repo,
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 0, f"{case_id} patch did not fix:\n{proc.stdout}\n{proc.stderr}"

    @pytest.mark.parametrize("case_id", READY_CASES)
    def test_min_lines_is_positive_int(self, case_id):
        text = (CASES_DIR / case_id / "min_lines.txt").read_text(encoding="utf-8").strip()
        assert int(text) >= 1
