"""Public pytest/unittest references resolve without guessing or escaping the repo."""

import pytest

from src.repair.verification.test_references import (
    normalize_related_test_refs,
    resolve_test_ref_for_pytest,
)


def _write(root, rel, text="def test_value():\n    pass\n"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_nodeid_parameters_and_windows_separators_are_preserved(tmp_path):
    _write(tmp_path, "tests/test_value.py")
    assert (
        resolve_test_ref_for_pytest("tests\\test_value.py::test_value[a/b]", tmp_path)
        == "tests/test_value.py::test_value[a/b]"
    )


def test_unittest_reference_and_equivalent_nodeid_dedupe(tmp_path):
    _write(tmp_path, "tests/pkg/test_value.py", "class ValueTests:\n    pass\n")
    assert normalize_related_test_refs(
        [
            "test_value (pkg.test_value.ValueTests)",
            "tests/pkg/test_value.py::ValueTests::test_value",
        ],
        tmp_path,
    ) == ["tests/pkg/test_value.py::ValueTests::test_value"]


def test_only_unique_suffixes_can_relocate(tmp_path):
    _write(tmp_path, "one/pkg/test_value.py")
    _write(tmp_path, "two/other/test_value.py")
    assert (
        resolve_test_ref_for_pytest("prefix/pkg/test_value.py::test_value", tmp_path)
        == "one/pkg/test_value.py::test_value"
    )
    assert resolve_test_ref_for_pytest("missing/test_value.py::test_value", tmp_path) == ""


@pytest.mark.parametrize("reference", ["../outside.py::test_value", "missing.py::test_value"])
def test_outside_or_missing_targets_are_unresolved(tmp_path, reference):
    _write(tmp_path, "outside.py")
    assert resolve_test_ref_for_pytest(reference, tmp_path) == ""


def test_absolute_inside_path_is_normalized_and_outside_rejected(tmp_path):
    inside = _write(tmp_path, "test_inside.py")
    assert resolve_test_ref_for_pytest(f"{inside}::test_value", tmp_path) == (
        "test_inside.py::test_value"
    )
    assert resolve_test_ref_for_pytest(f"{tmp_path.parent}/outside.py::test_value", tmp_path) == ""


def test_bare_names_ignore_nested_functions_comments_and_non_test_classes(tmp_path):
    _write(
        tmp_path,
        "test_noise.py",
        (
            "# def test_value():\n"
            "def helper():\n    def test_value():\n        pass\n"
            "class Helper:\n    def test_value(self):\n        pass\n"
        ),
    )
    _write(tmp_path, "value_test.py", "async def test_value():\n    pass\n")
    assert resolve_test_ref_for_pytest("test_value", tmp_path) == "value_test.py::test_value"


def test_ambiguous_methods_within_one_file_are_unresolved(tmp_path):
    _write(
        tmp_path,
        "test_value.py",
        (
            "class TestA:\n    def test_value(self):\n        pass\n"
            "class TestB:\n    def test_value(self):\n        pass\n"
        ),
    )
    assert resolve_test_ref_for_pytest("test_value", tmp_path) == ""


def test_unittest_class_name_need_not_start_with_test(tmp_path):
    _write(
        tmp_path,
        "test_value.py",
        (
            "import unittest\n"
            "class ValueChecks(unittest.TestCase):\n    def test_value(self):\n        pass\n"
        ),
    )
    assert resolve_test_ref_for_pytest("test_value", tmp_path) == (
        "test_value.py::ValueChecks::test_value"
    )


def test_hidden_dependency_trees_are_not_searched(tmp_path):
    _write(tmp_path, ".venv/test_value.py")
    _write(tmp_path, "test_value.py")
    assert resolve_test_ref_for_pytest("test_value", tmp_path) == "test_value.py::test_value"


def test_explicit_test_directory_remains_a_valid_target(tmp_path):
    _write(tmp_path, "tests/test_value.py")
    assert resolve_test_ref_for_pytest("tests", tmp_path) == "tests"
    assert resolve_test_ref_for_pytest("tests::test_value", tmp_path) == ""


def test_escaping_symlink_is_not_relocated_to_an_unrelated_file(tmp_path):
    root = tmp_path / "repo"
    _write(root, "safe/test_value.py")
    outside = tmp_path / "outside"
    _write(outside, "test_value.py")
    try:
        (root / "alias").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    assert resolve_test_ref_for_pytest("alias/test_value.py::test_value", root) == ""


@pytest.mark.parametrize("qualifier", [".", "pkg..ValueChecks", "pkg."])
def test_malformed_unittest_qualifier_is_unresolved(tmp_path, qualifier):
    assert resolve_test_ref_for_pytest(f"test_value ({qualifier})", tmp_path) == ""


def test_no_repo_keeps_reference_syntax_and_batch_order():
    assert normalize_related_test_refs(
        ["", None, "test_value (pkg.test_value.ValueChecks)", "test_other", "test_other"]
    ) == ["pkg/test_value.py::ValueChecks::test_value", "test_other"]
