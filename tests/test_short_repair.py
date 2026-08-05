"""短修快路径：单上下文加深。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from src.repair.short_repair import (
    ShortRepairDecision,
    build_workspace_brief,
    detect_short_repair,
    filter_suspects_for_short_repair,
    pop_patcher_depth,
    push_patcher_depth,
)
from src.state import RepairPlan, RepairState, RetrievedContext, SuspectLocation, VerificationResult


def _repo_with_files() -> tuple[Path, str]:
    raw = tempfile.mkdtemp(prefix="fixloop-short-")
    root = Path(raw)
    impl = root / "pkg" / "mod.py"
    impl.parent.mkdir(parents=True)
    impl.write_text(
        "\n".join(
            [
                "def add(a, b):",
                "    return a - b",
                "",
            ]
        ),
        encoding="utf-8",
    )
    test = root / "pkg" / "tests" / "test_mod.py"
    test.parent.mkdir(parents=True)
    test.write_text(
        "\n".join(
            [
                "from pkg.mod import add",
                "",
                "def test_add():",
                "    assert add(1, 2) == 3",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return root, "pkg/tests/test_mod.py::test_add"


class TestDetectShortRepair:
    def test_enabled_with_fail_surface_and_impl(self):
        root, nodeid = _repo_with_files()
        state = RepairState(issue_input="add is wrong")
        state.suspect_locations = [
            SuspectLocation(
                file_path="pkg/mod.py",
                start_line=1,
                end_line=2,
                reason="stack",
                confidence=0.9,
            )
        ]
        state.node_timings["verify_failed_nodeids"] = [nodeid]
        state.node_timings["verify_bucket"] = "logic"
        state.verification_result = VerificationResult(
            all_passed=False,
            failed=1,
            total_tests=1,
            failure_logs=[f"FAILED {nodeid} - AssertionError"],
        )
        d = detect_short_repair(state, root)
        assert d.enabled
        assert "pkg/mod.py" in d.impl_files
        assert d.max_steps >= 14

    def test_blocked_on_env_bucket(self):
        root, nodeid = _repo_with_files()
        state = RepairState(issue_input="x")
        state.suspect_locations = [
            SuspectLocation(file_path="pkg/mod.py", start_line=1, end_line=1)
        ]
        state.node_timings["verify_bucket"] = "env"
        state.node_timings["verify_failed_nodeids"] = [nodeid]
        d = detect_short_repair(state, root)
        assert not d.enabled
        assert d.reason == "blocked_by_bucket"

    def test_force_short_repair_flag(self):
        root, _nodeid = _repo_with_files()
        state = RepairState(issue_input="x")
        state.node_timings["force_short_repair"] = True
        state.suspect_locations = [
            SuspectLocation(file_path="pkg/mod.py", start_line=1, end_line=1, reason="grep命中"),
        ]
        d = detect_short_repair(state, root)
        assert d.enabled
        assert d.reason == "force_mid_tier"


class TestWorkspaceBrief:
    def test_brief_contains_test_and_impl(self):
        root, nodeid = _repo_with_files()
        state = RepairState(issue_input="x")
        state.suspect_locations = [
            SuspectLocation(file_path="pkg/mod.py", start_line=1, end_line=2)
        ]
        decision = ShortRepairDecision(
            enabled=True,
            reason="fail_surface",
            impl_files=["pkg/mod.py"],
            test_nodeids=[nodeid],
            max_steps=18,
        )
        brief = build_workspace_brief(decision, state, root)
        assert "SHORT REPAIR" in brief
        assert "test_add" in brief
        assert "def add" in brief
        assert "patch_file" in brief


class TestFilterAndDepth:
    def test_filter_keeps_impl_only(self):
        d = ShortRepairDecision(
            enabled=True,
            impl_files=["a.py"],
            reason="x",
        )
        suspects = [
            SuspectLocation(file_path="a.py", start_line=1, end_line=1),
            SuspectLocation(file_path="b.py", start_line=1, end_line=1),
        ]
        kept = filter_suspects_for_short_repair(suspects, d)
        assert [s.file_path for s in kept] == ["a.py"]

    def test_push_pop_max_steps(self):
        agent = SimpleNamespace(config=SimpleNamespace(max_steps=10))
        token = push_patcher_depth(agent, 18)
        assert agent.config.max_steps == 18
        pop_patcher_depth(agent, token)
        assert agent.config.max_steps == 10

    def test_push_can_lower_max_steps(self):
        agent = SimpleNamespace(config=SimpleNamespace(max_steps=12))
        token = push_patcher_depth(agent, 8)
        assert agent.config.max_steps == 8
        pop_patcher_depth(agent, token)
        assert agent.config.max_steps == 12
