"""F2P → 符号索引空锚种子。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from src.repair.fail_to_pass_hints import FAIL_TO_PASS_HEADER, extract_fail_to_pass_hints
from src.repair.localize_fastpath import rule_first_suspects, suspects_from_fail_to_pass
from src.repair.symbol_index import _INDEX_CACHE


def _tiny_repo() -> Path:
    raw = tempfile.mkdtemp(prefix="fixloop-f2p-")
    root = Path(raw)
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text(
        "def do_work(x):\n    return x + 1\n",
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_mod.py").write_text(
        "from pkg.mod import do_work\n\n"
        "def test_do_work():\n"
        "    assert do_work(1) == 2\n",
        encoding="utf-8",
    )
    _INDEX_CACHE.clear()
    return root


def test_extract_fail_to_pass_hints():
    issue = (
        "Bug somewhere\n\n"
        f"{FAIL_TO_PASS_HEADER}\n"
        "- tests/test_mod.py::test_do_work\n"
        "- tests/other.py::test_x\n"
    )
    assert extract_fail_to_pass_hints(issue) == [
        "tests/test_mod.py::test_do_work",
        "tests/other.py::test_x",
    ]


def test_suspects_from_f2p_seed_impl():
    root = _tiny_repo()
    issue = (
        "No stack frames here.\n\n"
        f"{FAIL_TO_PASS_HEADER}\n"
        "- tests/test_mod.py::test_do_work\n"
    )
    seeds = suspects_from_fail_to_pass(issue, root)
    paths = [s.file_path.replace("\\", "/") for s in seeds]
    assert "pkg/mod.py" in paths
    assert any(s.reason == "F2P覆盖" for s in seeds)


def test_rule_first_with_only_f2p():
    root = _tiny_repo()
    issue = (
        "Fix do_work regression.\n\n"
        f"{FAIL_TO_PASS_HEADER}\n"
        "- tests/test_mod.py::test_do_work\n"
    )
    suspects = rule_first_suspects(issue, root)
    paths = [s.file_path.replace("\\", "/") for s in suspects]
    assert "pkg/mod.py" in paths
