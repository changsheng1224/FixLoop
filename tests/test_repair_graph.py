"""Behavioral stage graph tests with unchanged FixLoop state and policies."""

import threading
from unittest.mock import Mock

import pytest

pytest.importorskip("langgraph")

from agent_runtime.cancellation import CancellationToken
from src.repair.attempt_policy import RepairAttemptPolicy
from src.repair.critic import CriticVerdict
from src.repair.graph import run_repair_graph
from src.repair.phase_clock import PhaseTimeoutConfig, PhaseTimeoutError, RepairPhaseClock
from src.repair.progress import ProgressEmitter
from src.repair.run_context import RepairRunContext
from src.repair.session import ExecutionSession
from src.state import CandidatePatch, RepairState, VerificationResult


@pytest.fixture
def graph_case(tmp_path, monkeypatch):
    session = ExecutionSession(None)
    session.repo_root = str(tmp_path)
    session.critic_mode = "off"
    session.repair_ctx = RepairRunContext(cancel_token=CancellationToken())
    session.progress = ProgressEmitter(quiet=True)
    state = RepairState(issue_input="Fix value", max_retries=2)
    calls = []
    owner_thread = threading.get_ident()

    def patch(current):
        assert current is state and threading.get_ident() == owner_thread
        calls.append("patch")
        return (
            [
                CandidatePatch(
                    file_path="value.py", original_lines="value = 1", patched_lines="value = 2"
                )
            ],
            {"total_ms": 1},
        )

    def verify(current):
        assert current is state and threading.get_ident() == owner_thread
        calls.append("verify")
        return VerificationResult(
            all_passed=True, total_tests=1, passed=1, execution_status="passed"
        )

    monkeypatch.setattr(session.exploration, "prepare_repair", lambda _: calls.append("prepare"))
    monkeypatch.setattr(session.patcher_execution, "run_patcher", patch)
    monkeypatch.setattr(session.verification, "run_verifier", verify)
    monkeypatch.setattr(session.verification, "verification_enabled", lambda: True)
    monkeypatch.setattr(
        session.verification, "record_validation_exit", lambda *_: calls.append("exit")
    )
    monkeypatch.setattr(session.asks, "record_l2_synthetic_ask", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        session.transaction, "restore_repo_snapshot", lambda _: calls.append("rollback")
    )
    monkeypatch.setattr(session.events, "checkpoint_progress", lambda _: calls.append("checkpoint"))
    return session, state, calls


@pytest.mark.parametrize("resume", [False, True])
def test_stage_graph_uses_same_state_owner_and_resume_entry(graph_case, resume):
    session, state, calls = graph_case
    assert not run_repair_graph(session, state, {}, None, resume=resume)
    assert calls == (["patch", "verify"] if resume else ["prepare", "patch", "verify"])
    assert state.status == "fixed" and state.retry_count == 0


def test_failed_verification_rolls_back_before_current_feedback_and_retry(graph_case, monkeypatch):
    from src.repair.failure_ledger import load_ledger_from_state

    session, state, calls = graph_case
    verify = session.verification.run_verifier

    def first_failure(current):
        if current.retry_count:
            assert current.feedback == "current evidence"
            return verify(current)
        calls.append("verify-failed")
        return VerificationResult(
            all_passed=False,
            total_tests=1,
            failed=1,
            execution_status="failed",
            failure_logs=["AssertionError: value differs"],
        )

    def feedback(result, *, state):
        assert calls[-1] == "rollback"
        ledger = load_ledger_from_state(state)
        assert ledger.hypotheses[0].counterexamples == ["AssertionError: value differs"]
        calls.append("feedback")
        return "current evidence"

    monkeypatch.setattr(session.verification, "run_verifier", first_failure)
    monkeypatch.setattr(session.patcher_execution, "build_feedback", feedback)
    run_repair_graph(session, state, {}, None)
    assert calls == [
        "prepare",
        "patch",
        "verify-failed",
        "exit",
        "rollback",
        "feedback",
        "checkpoint",
        "patch",
        "verify",
    ]
    assert state.retry_count == 1 and state.status == "fixed"


def test_critic_rejection_rolls_back_and_counts_once(graph_case, monkeypatch):
    session, state, calls = graph_case
    review = Mock(
        side_effect=[
            CriticVerdict(False, "scope", "rules_first"),
            CriticVerdict(True, "ok", "rules_first"),
        ]
    )
    monkeypatch.setattr("src.repair.critic.review_patch", review)
    run_repair_graph(session, state, {}, None)
    assert calls == ["prepare", "patch", "rollback", "checkpoint", "patch", "verify"]
    assert state.retry_count == 1 and review.call_count == 2


@pytest.mark.parametrize("terminal,expected_retries", [("cannot_patch", 0), ("", 1)])
def test_empty_patch_uses_original_terminal_and_retry_policy(
    graph_case, monkeypatch, terminal, expected_retries
):
    session, state, calls = graph_case
    patch = session.patcher_execution.run_patcher

    def empty_once(current):
        if current.retry_count:
            return patch(current)
        calls.append("empty")
        current.control.patcher_terminal_status = terminal
        return [], {"total_ms": 1}

    monkeypatch.setattr(session.patcher_execution, "run_patcher", empty_once)
    run_repair_graph(session, state, {}, None)
    assert state.retry_count == expected_retries
    assert calls.count("checkpoint") == 1
    assert calls.count("verify") == expected_retries


@pytest.mark.parametrize(
    "outcome,status,cancelled",
    [
        ("environment_unavailable", "pending_verify", False),
        ("unverified", "pending_verify", False),
        ("timed_out", "timeout", False),
        ("cancelled", "pending", True),
    ],
)
def test_non_test_outcomes_never_trigger_another_patch(
    graph_case, monkeypatch, outcome, status, cancelled
):
    session, state, calls = graph_case
    monkeypatch.setattr(
        session.verification,
        "run_verifier",
        lambda _: VerificationResult(all_passed=False, execution_status=outcome),
    )
    assert run_repair_graph(session, state, {}, None) is cancelled
    assert state.status == status and state.retry_count == 0
    assert calls == ["prepare", "patch"]


def test_protected_interfaces_are_checked_even_when_verification_disabled(graph_case, monkeypatch):
    session, state, calls = graph_case
    session.repair_config.protected_interfaces = {"value.py": ["value"]}
    monkeypatch.setattr(
        session.patcher_execution,
        "run_patcher",
        lambda _: (
            [
                CandidatePatch(
                    file_path="value.py",
                    original_lines="def value():\n    return 1",
                    patched_lines="def changed():\n    return 2",
                )
            ],
            {"total_ms": 1},
        ),
    )
    monkeypatch.setattr(session.verification, "verification_enabled", lambda: False)
    run_repair_graph(session, state, {}, None)
    assert state.retry_count == 2 and "protected_interface" in state.agent_errors
    assert calls == ["prepare", "rollback", "checkpoint", "rollback", "checkpoint"]


def test_phase_clock_accumulates_across_graph_retries(graph_case, monkeypatch):
    session, state, _ = graph_case
    patch = session.patcher_execution.run_patcher

    def slow_empty(current):
        patch(current)
        return [], {"total_ms": 600}

    monkeypatch.setattr(session.patcher_execution, "run_patcher", slow_empty)
    clock = RepairPhaseClock(PhaseTimeoutConfig(0, 1, 0, 0))
    with pytest.raises(PhaseTimeoutError, match="patch"):
        run_repair_graph(session, state, {}, clock)
    assert state.retry_count == 1


def test_graph_step_guard_covers_large_legal_retry_budget(graph_case, monkeypatch):
    session, state, calls = graph_case
    state.max_retries = 300
    monkeypatch.setattr(RepairAttemptPolicy, "run_critic_gate", lambda *_: True)
    run_repair_graph(session, state, {}, None)
    assert state.retry_count == 300
    assert calls.count("patch") == calls.count("rollback") == calls.count("checkpoint") == 300
    assert "verify" not in calls


def test_pre_cancelled_graph_does_not_prepare_or_patch(graph_case):
    session, state, calls = graph_case
    session.repair_ctx.cancel_token.cancel("user")
    assert run_repair_graph(session, state, {}, None)
    assert not calls
