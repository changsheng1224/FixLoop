"""符号索引 + grounded 门禁。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.repair.localization.localize_quality import (
    ensure_grounded_suspects,
    has_grounded_impl_suspect,
    refine_suspects,
)
from src.repair.localization.symbol_index import (
    boost_suspects_from_index,
    build_symbol_index,
    get_or_build_index,
)
from src.state import SuspectLocation


def _layout() -> Path:
    raw = tempfile.mkdtemp(prefix="fixloop-symidx-")
    root = Path(raw)
    pkg = root / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text(
        "\n".join(
            [
                "def compute_total(xs):",
                "    return sum(xs)",
                "",
                "class Accumulator:",
                "    def add(self, x):",
                "        return x",
                "",
            ]
        ),
        encoding="utf-8",
    )
    tests = root / "tests"
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
    return root


class TestSymbolIndex:
    def test_builds_defs_and_test_cover_edges(self):
        root = _layout()
        idx = build_symbol_index(root)
        assert idx.file_count >= 2
        hits = idx.lookup("compute_total")
        assert hits
        assert hits[0].path == "mypkg/core.py"
        covered = idx.impls_for_test("tests/test_core.py::test_compute_total")
        assert any(s.file_path == "mypkg/core.py" for s in covered)
        assert any(s.reason == "测试覆盖边" for s in covered)

    def test_boost_from_issue_symbol(self):
        root = _layout()
        # clear cache for this root via build path inside boost
        get_or_build_index(root)
        boosted = boost_suspects_from_index(
            repo_root=root,
            issue="bug in compute_total",
            related_tests=["tests/test_core.py::test_compute_total"],
        )
        assert any(s.file_path == "mypkg/core.py" for s in boosted)


class TestGroundedGate:
    def test_has_grounded_requires_real_impl_file(self):
        root = _layout()
        assert not has_grounded_impl_suspect(
            [SuspectLocation(file_path="missing.py", start_line=1, end_line=1)],
            root,
        )
        assert has_grounded_impl_suspect(
            [SuspectLocation(file_path="mypkg/core.py", start_line=1, end_line=1)],
            root,
        )

    def test_ensure_boosts_when_empty(self):
        root = _layout()
        suspects, boosted = ensure_grounded_suspects(
            [],
            repo_root=root,
            issue="compute_total fails",
            related_tests=["tests/test_core.py::test_compute_total"],
            fail_nodeids=["tests/test_core.py::test_compute_total"],
        )
        assert boosted
        assert has_grounded_impl_suspect(suspects, root)

    def test_refine_includes_index_hits(self):
        root = _layout()
        kept = refine_suspects(
            [],
            "assert compute_total broke",
            root,
            related_tests=["tests/test_core.py::test_compute_total"],
            fail_nodeids=["tests/test_core.py::test_compute_total"],
        )
        assert any("mypkg/core.py" in (s.file_path or "") for s in kept)
