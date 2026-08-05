"""可恢复长程：策略切换与 checkpoint 恢复。"""

from __future__ import annotations

from pathlib import Path

from src.repair.checkpoint_load import load_repair_checkpoint, save_repair_checkpoint
from src.repair.long_horizon import (
    LongHorizonController,
    StrategyPhase,
    apply_horizon_to_state,
    clear_soft_stop_flags,
    load_horizon_from_state,
    reset_stop_loss_tracker,
)
from src.repair.stop_loss import StopLossReason, StopLossTracker
from src.state import RepairPlan, RepairState, SuspectLocation


class TestLongHorizonController:
    def test_identical_patch_triggers_expand(self):
        ctrl = LongHorizonController(max_shifts=2)
        d = ctrl.on_stop_signal(StopLossReason.IDENTICAL_PATCH)
        assert d.action == "shift"
        assert d.phase == StrategyPhase.EXPAND_SEARCH
        assert ctrl.shifts_used == 1

    def test_identical_verify_triggers_switch(self):
        ctrl = LongHorizonController(max_shifts=2)
        d = ctrl.on_stop_signal(StopLossReason.IDENTICAL_VERIFY)
        assert d.action == "shift"
        assert d.phase == StrategyPhase.SWITCH_HYPOTHESIS

    def test_env_stops_immediately(self):
        ctrl = LongHorizonController(max_shifts=2)
        d = ctrl.on_stop_signal(StopLossReason.ENV)
        assert d.action == "stop"
        assert ctrl.shifts_used == 0

    def test_switch_feedback_tells_change_hypothesis(self):
        from src.repair.long_horizon import strategy_feedback

        ctrl = LongHorizonController(max_shifts=2)
        d = ctrl.on_stop_signal(StopLossReason.IDENTICAL_VERIFY)
        tip = strategy_feedback(d)
        assert "换假设" in tip
        assert "禁止再提交相同 fingerprint" in tip

    def test_shift_budget_exhausted(self):
        ctrl = LongHorizonController(max_shifts=1)
        assert ctrl.on_stop_signal("no_progress").action == "shift"
        d2 = ctrl.on_stop_signal("no_progress")
        assert d2.action == "stop"
        assert ctrl.phase == StrategyPhase.EXHAUSTED

    def test_roundtrip_state(self):
        state = RepairState(issue_input="x")
        ctrl = LongHorizonController(max_shifts=2)
        ctrl.on_stop_signal("identical_patch")
        apply_horizon_to_state(state, ctrl)
        loaded = load_horizon_from_state(state)
        assert loaded.shifts_used == 1
        assert loaded.phase == StrategyPhase.EXPAND_SEARCH


class TestResetAndClear:
    def test_reset_tracker_clears_streaks(self):
        tr = StopLossTracker()
        tr.record_empty_patch(apply_failed=False)
        tr2 = reset_stop_loss_tracker(tr)
        assert tr2.snapshot()["parse_fail_streak"] == 0

    def test_clear_soft_flags(self):
        state = RepairState(issue_input="x")
        state.node_timings["stop_loss"] = "identical_patch"
        state.node_timings["stop_loss_early"] = True
        state.agent_errors["stop_loss"] = "x"
        state.node_timings["patch_retry_fingerprints"] = {"a": 2}
        clear_soft_stop_flags(state)
        assert "stop_loss" not in state.node_timings
        assert "patch_retry_fingerprints" not in state.node_timings


class TestCheckpointRestoreLongHorizon:
    def test_restore_keeps_timings_and_strategy(self, tmp_path: Path):
        from src.repair.pipeline import RepairPipelineMixin

        state = RepairState(issue_input="bug", max_retries=5)
        state.repair_run_id = "lh-001"
        state.retry_count = 2
        state.phase = "patch"
        state.status = "exhausted"
        state.suspect_locations = [
            SuspectLocation(file_path="a.py", start_line=1, end_line=1)
        ]
        state.repair_plan = RepairPlan(issue_type="type_error")
        state.node_timings = {
            "verify_failed_nodeids": ["a.py::test_x"],
            "long_horizon": {"max_shifts": 2, "shifts_used": 1, "phase": "reconverge", "history": []},
            "stop_loss_early": True,
            "stop_loss": "identical_patch",
        }
        (tmp_path / ".agent" / "runs" / "lh-001").mkdir(parents=True)
        save_repair_checkpoint(state, str(tmp_path))

        loaded = load_repair_checkpoint(str(tmp_path), "lh-001")
        assert loaded is not None
        new_state = RepairState(issue_input="bug")
        new_state.repair_run_id = "lh-001"
        RepairPipelineMixin()._restore_state_from_repair_checkpoint(new_state, loaded)

        assert new_state.retry_count == 2
        assert new_state.status == "pending"
        assert new_state.node_timings.get("verify_failed_nodeids") == ["a.py::test_x"]
        assert new_state.node_timings.get("long_horizon", {}).get("shifts_used") == 1
        assert "stop_loss_early" not in new_state.node_timings
