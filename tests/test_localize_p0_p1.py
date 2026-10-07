"""P0/P1 localize：公开测试、cheap explore、tiers、landing、memory、LLM disk filter。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agent_runtime.tool_result import ToolResult
from src.repair.localization.localize_cheap_explore import cheap_explore_suspects
from src.repair.localization.localize_fastpath import (
    filter_llm_suspects_to_disk,
)
from src.repair.localization.localize_landing import refine_suspect_landing
from src.repair.localization.localize_memory import (
    apply_localize_memory,
    remember_confirmed_impls,
    remember_negated_files,
)
from src.repair.localization.localize_tiers import SuspectTier, decide_patch_gate, tier_for_suspect
from src.state import RepairState, SuspectLocation
from tests.repair_support import build_repository


def _repo(root: Path) -> Path:
    return build_repository(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/core.py": "def compute(x):\n    return x + 1\n",
            "tests/test_core.py": "from pkg.core import compute\n\ndef test_compute():\n    assert compute(1) == 2\n",
        },
    )


def test_cheap_explore_grep_hits(tmp_path):
    root = _repo(tmp_path)
    issue = "Bug in compute function returning wrong value"
    with patch("agent_runtime.tools.tool_grep") as g:
        g.return_value = ToolResult(content="pkg/core.py:1:def compute(x):")
        hits = cheap_explore_suspects(issue, root, max_keywords=4)
    paths = [s.file_path.replace("\\", "/") for s in hits]
    assert "pkg/core.py" in paths
    assert hits[0].reason == "grep命中"


def test_tier_gate_mid_forces_short(tmp_path):
    root = _repo(tmp_path)
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


def test_tier_gate_blocks_test_only(tmp_path):
    root = _repo(tmp_path)
    low = SuspectLocation(
        file_path="tests/test_core.py",
        start_line=1,
        end_line=1,
        reason="关联测试",
        confidence=0.45,
    )
    d = decide_patch_gate([low], root)
    assert not d.allow
    assert d.reason == "no_editable_impl"


def test_tier_gate_low_impl_allows(tmp_path):
    root = _repo(tmp_path)
    low = SuspectLocation(
        file_path="pkg/core.py",
        start_line=1,
        end_line=1,
        reason="weak",
        confidence=0.2,
    )
    d = decide_patch_gate([low], root)
    assert d.allow and d.force_short_repair


def test_filter_llm_requires_disk(tmp_path):
    root = _repo(tmp_path)
    llm = [
        SuspectLocation(file_path="pkg/core.py", start_line=1, end_line=1, reason="llm"),
        SuspectLocation(file_path="pkg/missing.py", start_line=1, end_line=1, reason="llm"),
    ]
    kept = filter_llm_suspects_to_disk(llm, root)
    assert [s.file_path.replace("\\", "/") for s in kept] == ["pkg/core.py"]


def test_landing_sets_line_from_symbol(tmp_path):
    root = _repo(tmp_path)
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


def test_memory_burn_and_confirm(tmp_path):
    root = _repo(tmp_path)
    state = RepairState(issue_input="x")
    state.node_timings["failure_ledger"] = {"negated_files": ["pkg/bad.py"]}
    remember_negated_files(state)
    sus = [
        SuspectLocation(
            file_path="pkg/core.py",
            start_line=1,
            end_line=1,
            reason="堆栈指向",
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
