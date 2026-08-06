"""Patcher runtime contract tests."""

from src.repair.execution.patcher_contract import (
    PatcherTerminalStatus,
    classify_patcher_attempt,
    record_patcher_terminal_status,
    render_patcher_runtime_contract,
)
from src.state import CandidatePatch, RepairState


def test_runtime_contract_renders_feedback_and_no_progress_controls():
    state = RepairState(issue_input="x")
    state.node_timings["structured_verify_feedback"] = {
        "bucket": "logic",
        "reason": "assertion failed",
        "failing_tests": ["tests/test_x.py::test_y"],
        "verify_target": "tests/test_x.py::test_y",
        "patch_files": ["pkg/x.py"],
        "next_action": "read_failed_test_then_patch_minimal_impl_and_reverify_same_target",
    }
    state.node_timings["no_progress_warning"] = {
        "no_progress_count": 2,
        "required_next_action": "write_patch_or_expand_context",
        "forbid_repeated_reads": True,
        "allowed_next_actions": ["write_patch", "expand_context"],
    }

    block = render_patcher_runtime_contract(state)

    assert "PATCHER RUNTIME CONTRACT" in block
    assert "VERIFY FEEDBACK CONTRACT" in block
    assert "tests/test_x.py::test_y" in block
    assert "repeated reads are disallowed" in block


def test_classifies_and_records_terminal_status():
    state = RepairState(issue_input="x")
    status = classify_patcher_attempt(
        state,
        [CandidatePatch(file_path="a.py", diff="+x")],
    )
    record_patcher_terminal_status(state, status, reason="unit")

    assert status == PatcherTerminalStatus.PATCH_PRODUCED
    assert state.node_timings["patcher_terminal_status"] == "patch_produced"
    assert state.node_timings["patcher_terminal_history"][0]["reason"] == "unit"


def test_empty_parse_failure_is_model_output_invalid():
    state = RepairState(issue_input="x")
    state.agent_errors["patcher_parse"] = "parse_fail"

    assert classify_patcher_attempt(state, []) == PatcherTerminalStatus.MODEL_OUTPUT_INVALID
