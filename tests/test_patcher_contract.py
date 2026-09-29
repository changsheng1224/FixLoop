"""Patcher runtime contract tests."""

from src.repair.execution.patcher_contract import (
    PatcherPhase,
    PatcherTerminalStatus,
    begin_patcher_attempt,
    classify_patcher_attempt,
    derive_patcher_phase,
    patcher_evidence_snapshot,
    record_patcher_terminal_status,
    render_patcher_runtime_contract,
    terminal_status_from_answer,
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


def test_explicit_cannot_patch_is_not_parse_failure():
    state = RepairState(issue_input="x")
    state.agent_errors["patcher_parse"] = "parse_fail"

    status = classify_patcher_attempt(
        state,
        [],
        terminal_answer='<final>{"status":"cannot_patch","reason":"missing source"}</final>',
    )

    assert terminal_status_from_answer("cannot_patch: missing source") == (
        PatcherTerminalStatus.CANNOT_PATCH
    )
    assert status == PatcherTerminalStatus.CANNOT_PATCH


def test_missing_grounding_is_localization_incomplete():
    state = RepairState(issue_input="x")

    assert patcher_evidence_snapshot(state)["grounded"] is False
    assert classify_patcher_attempt(state, []) == PatcherTerminalStatus.LOCALIZATION_INCOMPLETE


def test_no_change_is_a_distinct_terminal_cause_and_phase():
    state = RepairState(issue_input="x")
    state.node_timings["patch_no_change"] = True
    state.node_timings["patcher_terminal_status"] = "no_change"

    assert classify_patcher_attempt(state, []) == PatcherTerminalStatus.NO_CHANGE
    assert derive_patcher_phase(state) == PatcherPhase.TERMINAL


def test_runtime_contract_exposes_phase_and_evidence_summary():
    state = RepairState(issue_input="x")
    state.suspect_locations = []
    block = render_patcher_runtime_contract(state)

    assert "internal_phase: locating" in block
    assert "grounded=False" in block


def test_begin_attempt_clears_transient_status_but_keeps_history():
    state = RepairState(issue_input="x")
    state.node_timings.update(
        {
            "patcher_terminal_status": "no_change",
            "patcher_terminal_reason": "same content",
            "patch_no_change": True,
            "patcher_terminal_history": [{"status": "no_change"}],
        }
    )
    state.agent_errors["patcher_apply"] = "old error"

    begin_patcher_attempt(state)

    assert "patcher_terminal_status" not in state.node_timings
    assert "patch_no_change" not in state.node_timings
    assert state.node_timings["patcher_terminal_history"] == [{"status": "no_change"}]
    assert "patcher_apply" not in state.agent_errors
