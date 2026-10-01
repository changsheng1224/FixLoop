"""Shared Plan projections and pure replan gates, using actual journals."""

from types import SimpleNamespace

import pytest

from src.repair.replan_decision import decide_replan
from src.state import VerificationResult
from tests.plan_support import read, session_for, simple_plan


def test_view_context_and_tool_share_snapshot_without_mutation(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        attempt = session.prepare("read-0")
        session.local.attempt = attempt
        before = session.plan.to_dict(), session.long_task_state.to_dict(), session.store.events()
        view = session.plan_view("read-0")
        context = session.build_long_task_context()
        assert context["plan_view"] == view
        assert context["current_node"] == view["nodes"][0]
        assert (
            session.plan.to_dict(),
            session.long_task_state.to_dict(),
            session.store.events(),
        ) == before
        result = read(session, attempt)
        op = next(iter(session.store.latest("operation", "operation_id").values()))
        assert op["plan_view"] == view
        view["nodes"][0]["objective"] = "tampered display"
        with pytest.raises(ValueError, match="stale_or_modified"):
            session.validate_plan_view(view)
        session.settle(session.record_result(attempt, result))


def test_parallel_context_requires_selected_node_and_rejects_old_view(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session, reads=2))
        first = session.prepare("read-0")
        old = session.plan_view("read-0")
        second = session.prepare("read-1")
        with pytest.raises(ValueError, match="node_required"):
            session.build_long_task_context()
        assert session.plan_view()["active_node_ids"] == ["read-0", "read-1"]
        with pytest.raises(ValueError, match="stale_or_modified"):
            session.validate_plan_view(old)
        session.local.attempt = second
        assert session.build_long_task_context()["current_node"]["node_id"] == "read-1"
        with pytest.raises(ValueError, match="node_mismatch"):
            session.build_long_task_context("read-0")
        for attempt in (first, second):
            session.local.attempt = attempt
            session.settle(session.record_result(attempt, read(session, attempt)))
        session.local.attempt = first
        called = []
        with pytest.raises(ValueError, match="attempt_stale"):
            session.execute_tool(
                SimpleNamespace(session={}),
                "read_file",
                {"path": "value.py"},
                lambda: called.append(1),
            )
        assert not called


def test_stale_context_projection_leaves_authoritative_state_unchanged(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.run_node("read-0", lambda a: read(session, a))
        (tmp_path / "value.py").write_text("external change\n")
        before = session.long_task_state.seal(), session.plan, session.store.events()
        context = session.build_long_task_context("read-0")
        assert context["needs_evidence_refresh"]
        assert context["stale_evidence"]
        assert (session.long_task_state.seal(), session.plan, session.store.events()) == before


@pytest.mark.parametrize(
    "overrides,action,reason",
    [
        ({}, "replan", "confirmed_code_verification_failure"),
        ({"result": None}, "keep_plan", "no_confirmed_code_verification_failure"),
        (
            {"result": VerificationResult(total_tests=1, failure_logs=["ModuleNotFoundError"])},
            "keep_plan",
            "verification_env",
        ),
        (
            {"result": VerificationResult(failed=1, failure_logs=["AssertionError"])},
            "keep_plan",
            "verification_empty_collection",
        ),
        (
            {"result": VerificationResult(total_tests=1, failure_logs=["unknown failure"])},
            "keep_plan",
            "verification_unknown",
        ),
        ({"receipt": {"completed": False}}, "block", "verification_receipt_unconfirmed"),
        ({"safety_reason": "rollback_unconfirmed"}, "block", "rollback_unconfirmed"),
        ({"safety_reason": "cancelled", "stop_reason": "budget"}, "block", "cancelled"),
        ({"stop_reason": "stop_loss"}, "stop", "stop_loss"),
        ({"stop_reason": "deadline_exceeded"}, "stop", "deadline_exceeded"),
        ({"stop_reason": "global_budget_exhausted"}, "stop", "global_budget_exhausted"),
        ({"retry_allowed": False}, "stop", "orchestrator_retry_not_allowed"),
        ({"evidence_refs": []}, "needs_evidence", "fresh_repository_evidence_required"),
    ],
)
def test_replan_decisions(tmp_path, overrides, action, reason):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        values = dict(
            trigger_ref="receipt",
            result=VerificationResult(total_tests=1, failed=1),
            receipt={"completed": True, "command": "pytest"},
            evidence_refs=["fresh"],
            retry_allowed=True,
        )
        values.update(overrides)
        before = session.store.events()
        decision = decide_replan(session.plan_view(), **values)
        assert (decision.action, decision.reason) == (action, reason)
        assert session.store.events() == before


def test_running_and_replan_limit_take_priority(tmp_path):
    from dataclasses import replace

    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.commit(replace(session.plan, plan_version=3).seal())
        assert decide_replan(session.plan_view()).reason == "replan_budget_exceeded"
        session.prepare("read-0")
        assert decide_replan(session.plan_view()).action == "block"
