"""F2P 置顶进锁（种子护栏，无强制反思剧本）。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from src.orchestrator import Orchestrator
from src.repair.execution.lock_reflect import (
    f2p_impl_paths,
    merge_f2p_paths_first,
)
from src.state import RepairState


def test_f2p_impl_paths_prefers_neighbor_impl():
    raw = tempfile.mkdtemp(prefix="fixloop-f2p-")
    root = Path(raw)
    (root / "pkg" / "mod").mkdir(parents=True)
    (root / "pkg" / "mod" / "impl.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "pkg" / "mod" / "tests").mkdir()
    (root / "pkg" / "mod" / "tests" / "test_impl.py").write_text(
        "from pkg.mod.impl import f\n\ndef test_f():\n    assert f() == 1\n",
        encoding="utf-8",
    )
    issue = (
        "bug\n"
        "FAIL_TO_PASS tests (hints, may not exist locally):\n"
        "- pkg/mod/tests/test_impl.py::test_f\n"
    )
    paths = f2p_impl_paths(issue, root)
    assert any(p.endswith("impl.py") for p in paths) or any(
        "impl" in p for p in paths
    )


def test_merge_f2p_paths_first_keeps_f2p_when_filter_would_drop():
    f2p = ["astropy/modeling/separable.py"]
    noisy = ["astropy/constants/constant.py", "astropy/modeling/polynomial.py"]
    merged = merge_f2p_paths_first(f2p, noisy, max_keep=5)
    assert merged[0] == "astropy/modeling/separable.py"
    assert "astropy/modeling/separable.py" in merged


def test_seed_patcher_primary_includes_f2p_impl(monkeypatch):
    raw = tempfile.mkdtemp(prefix="fixloop-seed-f2p-")
    root = Path(raw)
    (root / "astropy" / "modeling").mkdir(parents=True)
    (root / "astropy" / "modeling" / "separable.py").write_text(
        "def sep():\n    return True\n", encoding="utf-8"
    )
    (root / "astropy" / "modeling" / "polynomial.py").write_text(
        "def p():\n    return 0\n", encoding="utf-8"
    )
    (root / "astropy" / "modeling" / "tests").mkdir()
    (root / "astropy" / "modeling" / "tests" / "test_separable.py").write_text(
        "def test_separable():\n    pass\n", encoding="utf-8"
    )

    monkeypatch.setenv("FIXLOOP_REPAIR_MODE", "patcher_primary")
    orch = Orchestrator(None)
    orch._repo_root = str(root)
    orch._progress = MagicMock()
    orch._progress.emit = MagicMock()

    def fake_seed(state, repo_root, **kwargs):
        from src.state import SuspectLocation

        state.suspect_locations = [
            SuspectLocation(
                file_path="astropy/modeling/polynomial.py",
                start_line=1,
                end_line=1,
                reason="test_patch覆盖",
                confidence=0.9,
            )
        ]
        return list(state.suspect_locations)

    monkeypatch.setattr(
        "src.repair.localization.localize_fastpath.seed_rule_first_suspects", fake_seed
    )

    state = RepairState(
        issue_input=(
            "sep bug\n"
            "FAIL_TO_PASS tests (hints, may not exist locally):\n"
            "- astropy/modeling/tests/test_separable.py::test_separable\n"
        )
    )
    orch._seed_patcher_primary(state)
    allowed = set(state.node_timings.get("allowed_edit") or [])
    assert "astropy/modeling/separable.py" in allowed
    assert state.node_timings.get("f2p_seeded") is True
