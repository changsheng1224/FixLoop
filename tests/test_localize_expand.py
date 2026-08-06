"""定位天花板：多跳语义扩展测试。"""

from __future__ import annotations

from pathlib import Path

from src.repair.localization.localize_expand import (
    expand_suspects_semantic,
    extract_symbols_from_issue,
    find_definitions,
    symbols_from_python_file,
)
from src.repair.localization.localize_quality import refine_suspects
from src.state import SuspectLocation


def _layout(tmp: Path) -> Path:
    pkg = tmp / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text(
        "\n".join(
            [
                "def compute_total(xs):",
                "    return sum(xs)",
                "",
                "def helper():",
                "    return compute_total([1, 2])",
                "",
            ]
        ),
        encoding="utf-8",
    )
    tests = tmp / "tests"
    tests.mkdir()
    (tests / "test_core.py").write_text(
        "\n".join(
            [
                "from mypkg.core import compute_total",
                "",
                "def test_compute_total():",
                "    assert compute_total([1, 2]) == 3",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return tmp


class TestSymbolsAndDefs:
    def test_symbols_from_test_file(self, tmp_path: Path):
        root = _layout(tmp_path)
        names, imports = symbols_from_python_file(
            root / "tests" / "test_core.py", focus_func="test_compute_total"
        )
        assert any(m == "mypkg.core" for m, _ in imports)
        assert "compute_total" in names

    def test_find_definition(self, tmp_path: Path):
        root = _layout(tmp_path)
        hits = find_definitions(root, "compute_total", prefer_dirs=["mypkg"])
        assert hits
        assert hits[0][0] == "mypkg/core.py"
        assert hits[0][2] == "function"

    def test_extract_issue_symbols(self):
        syms = extract_symbols_from_issue("ValueError in compute_total and FooBar")
        assert "compute_total" in syms
        assert "FooBar" in syms
        assert "ValueError" in syms or "error" not in [s.lower() for s in syms]


class TestExpandSemantic:
    def test_test_import_to_impl(self, tmp_path: Path):
        root = _layout(tmp_path)
        expanded = expand_suspects_semantic(
            [],
            repo_root=root,
            issue="assert failed",
            related_tests=["tests/test_core.py::test_compute_total"],
            max_new=6,
        )
        assert any(s.file_path == "mypkg/core.py" for s in expanded)
        assert any(s.reason in ("测试导入", "语义扩展", "测试导入模块") for s in expanded)

    def test_caller_expand(self, tmp_path: Path):
        root = _layout(tmp_path)
        seeds = [
            SuspectLocation(
                file_path="mypkg/core.py",
                start_line=1,
                end_line=2,
                function_name="compute_total",
                reason="堆栈指向",
                confidence=0.9,
            )
        ]
        expanded = expand_suspects_semantic(
            seeds,
            repo_root=root,
            issue="",
            max_new=6,
        )
        # helper() calls compute_total
        assert any(
            s.file_path == "mypkg/core.py" and s.reason == "调用方扩展" for s in expanded
        ) or any(s.reason == "调用方扩展" for s in expanded)


class TestRefineWithExpand:
    def test_refine_includes_semantic_hit(self, tmp_path: Path):
        root = _layout(tmp_path)
        refined = refine_suspects(
            [],
            "failed compute_total",
            root,
            related_tests=["tests/test_core.py::test_compute_total"],
            max_keep=8,
        )
        assert any(s.file_path == "mypkg/core.py" for s in refined)
