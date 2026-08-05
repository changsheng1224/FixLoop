"""R5→R6：产出 patch + FixLoop verify 链路根因修复回归。"""

from __future__ import annotations

from pathlib import Path

from src.benchmark.swebench.convert import (
    normalize_related_test_refs,
    resolve_test_ref_for_pytest,
)
from src.repair.patch_applier import (
    PatchApplier,
    apply_patch_to_text,
    normalize_patch_text_field,
    sibling_pattern_remains,
)
from src.repair.phase_clock import DEFAULT_PATCH_TIMEOUT_S
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
            {
                "file_path": "x.py",
                "original_lines": ["old"],
                "patched_lines": ["new"],
            }
        )
        assert p.original_lines == "old"
        assert p.patched_lines == "new"

    def test_apply_list_repr_patch(self):
        text = "                    stream.append(curr_stream)\n"
        patch = CandidatePatch(
            file_path="x.py",
            original_lines="['                    stream.append(curr_stream)']",
            patched_lines=(
                "['                    if curr_stream is not None:', "
                "'                        stream.append(curr_stream)']"
            ),
        )
        out = apply_patch_to_text(text, patch)
        assert out is not None
        assert "if curr_stream is not None" in out
        assert "['" not in out


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
            ["test_a (tests.test_a)", "tests/test_a.py::test_a"],
            repo,
        )
        assert len(refs) >= 1


class TestE6aSiblingReplaceAll:
    def test_replace_all_identical_snippets(self):
        text = (
            "class A:\n"
            "    regex = r'^[\\w.@+-]+$'\n"
            "class B:\n"
            "    regex = r'^[\\w.@+-]+$'\n"
        )
        patch = CandidatePatch(
            file_path="v.py",
            original_lines="    regex = r'^[\\w.@+-]+$'",
            patched_lines="    regex = r'^[\\w.@+-]+\\Z'",
        )
        out = apply_patch_to_text(text, patch)
        assert out is not None
        assert out.count("\\Z") == 2
        assert not sibling_pattern_remains(out, patch)

    def test_applier_writes_all_sites(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        f = repo / "v.py"
        f.write_text("x = 1\nx = 1\n", encoding="utf-8")
        applier = PatchApplier(str(repo))
        applied = applier.apply_patches(
            [CandidatePatch(file_path="v.py", original_lines="x = 1", patched_lines="x = 2")]
        )
        assert applied
        assert f.read_text(encoding="utf-8") == "x = 2\nx = 2\n"
        assert applier.last_sibling_warnings == []


class TestE14PatchBudget:
    def test_default_patch_budget_allows_swe_scale(self):
        assert DEFAULT_PATCH_TIMEOUT_S >= 300


class TestE6aCollapsedWhitespaceApply:
    def test_internal_whitespace_mismatch_still_applies(self):
        text = "    regex = r'^[\\w.@+-]+$'\n"
        patch = CandidatePatch(
            file_path="v.py",
            original_lines="    regex  =  r'^[\\w.@+-]+$'",
            patched_lines="    regex = r'^[\\w.@+-]+\\Z'",
        )
        out = apply_patch_to_text(text, patch)
        assert out is not None
        assert "\\Z" in out

    def test_hunk_mismatch_includes_near_lines(self, tmp_path: Path):
        from src.repair.patch_applier import describe_hunk_mismatch

        repo = tmp_path / "repo"
        repo.mkdir()
        f = repo / "v.py"
        f.write_text("    regex = r'^[\\w.@+-]+$'\n", encoding="utf-8")
        applier = PatchApplier(str(repo))
        applied = applier.apply_patches(
            [
                CandidatePatch(
                    file_path="v.py",
                    original_lines="    totally_unrelated = 42",
                    patched_lines="    totally_unrelated = 99",
                )
            ]
        )
        assert applied == []
        assert applier.last_apply_errors
        err = applier.last_apply_errors[0]
        assert "hunk_mismatch:v.py" in err
        assert "wanted" in err
        detail = describe_hunk_mismatch(
            f.read_text(encoding="utf-8"),
            CandidatePatch(
                file_path="v.py",
                original_lines="    regex = r'^NOMATCH$'",
                patched_lines="x",
            ),
        )
        assert "wanted" in detail
        assert "near=" in detail
