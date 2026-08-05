"""P0/P1 localize：test_patch、cheap explore、tiers、landing、memory、LLM disk filter。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from src.repair.fail_to_pass_hints import FAIL_TO_PASS_HEADER
from src.repair.localize_cheap_explore import cheap_explore_suspects
from src.repair.localize_fastpath import filter_llm_suspects_to_disk, rule_first_suspects
from src.repair.localize_landing import refine_suspect_landing
from src.repair.localize_memory import (
    apply_localize_memory,
    remember_confirmed_impls,
    remember_negated_files,
    save_localize_memory,
)
from src.repair.localize_test_patch import suspects_from_test_patch
from src.repair.localize_tiers import SuspectTier, decide_patch_gate, tier_for_suspect
from src.repair.symbol_index import _INDEX_CACHE
from src.state import RepairState, SuspectLocation


def _repo() -> Path:
    raw = tempfile.mkdtemp(prefix="fixloop-loc-p0-")
    root = Path(raw)
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text(
        "def compute(x):\n    return x + 1\n",
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_core.py").write_text(
        "from pkg.core import compute\n\n"
        "def test_compute():\n"
        "    assert compute(1) == 2\n",
        encoding="utf-8",
    )
    _INDEX_CACHE.clear()
    return root


def test_suspects_from_test_patch_imports():
    root = _repo()
    patch = """
diff --git a/tests/test_core.py b/tests/test_core.py
--- a/tests/test_core.py
+++ b/tests/test_core.py
@@ -1,3 +1,4 @@
 from pkg.core import compute
+from pkg.core import compute as c2
 
 def test_compute():
"""
    seeds = suspects_from_test_patch(patch, root)
    paths = [s.file_path.replace("\\", "/") for s in seeds]
    assert "pkg/core.py" in paths
    assert any(s.reason == "test_patch覆盖" for s in seeds)


def test_cheap_explore_grep_hits():
    root = _repo()
    issue = "Bug in compute function returning wrong value"
    with patch("agent_runtime.tools.tool_grep") as g:
        g.return_value = "pkg/core.py:1:def compute(x):"
        hits = cheap_explore_suspects(issue, root, max_keywords=4)
    paths = [s.file_path.replace("\\", "/") for s in hits]
    assert "pkg/core.py" in paths
    assert hits[0].reason == "grep命中"


def test_tier_gate_mid_forces_short():
    root = _repo()
    mid = SuspectLocation(
        file_path="pkg/core.py",
        start_line=1,
        end_line=1,
        reason="grep命中",
        confidence=0.58,
    )
    assert tier_for_suspect(mid, root) == SuspectTier.MID
    d = decide_patch_gate([mid], root)
    assert d.allow and d.force_short_repair


def test_tier_gate_blocks_test_only():
    root = _repo()
    low = SuspectLocation(
        file_path="tests/test_core.py",
        start_line=1,
        end_line=1,
        reason="F2P测试",
        confidence=0.45,
    )
    d = decide_patch_gate([low], root)
    assert not d.allow
    assert d.reason == "no_editable_impl"


def test_tier_gate_low_impl_allows():
    root = _repo()
    low = SuspectLocation(
        file_path="pkg/core.py",
        start_line=1,
        end_line=1,
        reason="weak",
        confidence=0.2,
    )
    d = decide_patch_gate([low], root)
    assert d.allow and d.force_short_repair


def test_filter_llm_requires_disk():
    root = _repo()
    llm = [
        SuspectLocation(file_path="pkg/core.py", start_line=1, end_line=1, reason="llm"),
        SuspectLocation(file_path="pkg/missing.py", start_line=1, end_line=1, reason="llm"),
    ]
    kept = filter_llm_suspects_to_disk(llm, root)
    assert [s.file_path.replace("\\", "/") for s in kept] == ["pkg/core.py"]


def test_landing_sets_line_from_symbol():
    root = _repo()
    rough = [
        SuspectLocation(
            file_path="pkg/core.py",
            start_line=1,
            end_line=1,
            reason="grep命中",
            confidence=0.6,
        )
    ]
    landed = refine_suspect_landing(rough, root, issue="fix compute please")
    assert landed[0].start_line >= 1
    assert landed[0].function_name in (None, "compute") or landed[0].start_line == 1


def test_memory_burn_and_confirm():
    root = _repo()
    state = RepairState(issue_input="x")
    state.node_timings["failure_ledger"] = {"negated_files": ["pkg/bad.py"]}
    remember_negated_files(state)
    sus = [
        SuspectLocation(
            file_path="pkg/core.py",
            start_line=1,
            end_line=1,
            reason="F2P覆盖",
            confidence=0.9,
        ),
        SuspectLocation(
            file_path="pkg/bad.py",
            start_line=1,
            end_line=1,
            reason="grep命中",
            confidence=0.6,
        ),
    ]
    remember_confirmed_impls(state, sus, repo_root=str(root))
    filtered = apply_localize_memory(sus, state)
    paths = [s.file_path.replace("\\", "/") for s in filtered]
    assert "pkg/core.py" in paths
    assert "pkg/bad.py" not in paths


def test_rule_first_with_test_patch_arg():
    root = _repo()
    issue = "Something broken.\n"
    patch = """
--- a/tests/test_core.py
+++ b/tests/test_core.py
@@ -1,2 +1,3 @@
 from pkg.core import compute
+from pkg.core import compute
"""
    suspects = rule_first_suspects(issue, root, test_patch=patch)
    paths = [s.file_path.replace("\\", "/") for s in suspects]
    assert "pkg/core.py" in paths
