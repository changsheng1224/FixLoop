"""R5→R6：产出 patch + FixLoop verify 链路根因修复回归。"""

from __future__ import annotations

from pathlib import Path

from src.repair.patch_artifacts import normalize_patch_text_field
from src.repair.phase_clock import DEFAULT_PATCH_TIMEOUT_S
from src.repair.verification.test_references import (
    normalize_related_test_refs,
    resolve_test_ref_for_pytest,
)
from src.state import CandidatePatch


class TestE19NormalizeLines:
    def test_list_field_joins(self):
        assert normalize_patch_text_field(["a", "b"]) == "a\nb"

    def test_list_repr_string_parsed(self):
        raw = "['                    stream.append(curr_stream)']"
        out = normalize_patch_text_field(raw)
        assert out == "                    stream.append(curr_stream)"
        assert not out.startswith("[")

    def test_from_dict_normalizes(self):
        p = CandidatePatch.from_dict(
            {"file_path": "x.py", "original_lines": ["old"], "patched_lines": ["new"]}
        )
        assert p.original_lines == "old"
        assert p.patched_lines == "new"


class TestE17FailToPassNormalize:
    def test_unittest_style_to_pytest(self, tmp_path: Path):
        repo = tmp_path / "repo"
        test_file = repo / "tests" / "auth_tests" / "test_validators.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text(
            "class UsernameValidatorsTests:\n    def test_ascii_validator(self):\n        pass\n",
            encoding="utf-8",
        )
        ref = "test_ascii_validator (auth_tests.test_validators.UsernameValidatorsTests)"
        got = resolve_test_ref_for_pytest(ref, repo)
        assert got.endswith(
            "tests/auth_tests/test_validators.py::UsernameValidatorsTests::test_ascii_validator"
        ) or got.endswith(
            "tests\\auth_tests\\test_validators.py::UsernameValidatorsTests::test_ascii_validator"
        )
        assert "::UsernameValidatorsTests::test_ascii_validator" in got.replace("\\", "/")

    def test_pytest_path_relocated_by_basename(self, tmp_path: Path):
        repo = tmp_path / "repo"
        real = repo / "lib" / "matplotlib" / "tests" / "test_backend_ps.py"
        real.parent.mkdir(parents=True)
        real.write_text("def test_empty_line():\n    pass\n", encoding="utf-8")
        ref = "wrong/prefix/test_backend_ps.py::test_empty_line"
        got = resolve_test_ref_for_pytest(ref, repo)
        assert got.replace("\\", "/").endswith(
            "lib/matplotlib/tests/test_backend_ps.py::test_empty_line"
        )

    def test_normalize_batch_dedupes(self, tmp_path: Path):
        repo = tmp_path / "repo"
        (repo / "tests").mkdir(parents=True)
        f = repo / "tests" / "test_a.py"
        f.write_text("def test_a():\n    pass\n", encoding="utf-8")
        refs = normalize_related_test_refs(
            ["test_a (tests.test_a)", "tests/test_a.py::test_a"], repo
        )
        assert len(refs) >= 1


class TestE14PatchBudget:
    def test_default_patch_budget_allows_swe_scale(self):
        assert DEFAULT_PATCH_TIMEOUT_S >= 300
