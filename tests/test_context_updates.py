"""Trusted provenance, explicit version chains and atomic owner publication."""

import os
import subprocess
import sys
import threading
from copy import deepcopy

import pytest

from agent_runtime.plan_runtime.constraint_records import constraint_history
from agent_runtime.plan_runtime.decisions import decision_history
from agent_runtime.plan_runtime.models import digest
from tests.plan_support import session_for, simple_plan, through_analysis, write


def seed(session):
    session.configure_long_task("修复原始问题，保留完整目标", hard_constraints=["Keep tests"])
    session.create(simple_plan(session))


def source(session, text, *, message_id="change", model=False):
    register = session.register_model_message if model else session.register_user_message
    register(message_id, text)
    return [session.source_reference(message_id)]


def constraint(refs, statement="Preserve API", **kwargs):
    return {
        "kind": "constraint",
        "action": "add",
        "statement": statement,
        "source_refs": refs,
        **kwargs,
    }


def submit(session, updates, batch_id="batch", **kwargs):
    return session.submit_context_updates(
        batch_id,
        updates=updates,
        expected_state_revision=kwargs.get("revision", session.long_task_state.state_revision),
        expected_plan_version=kwargs.get(
            "version", session.plan.plan_version if session.plan else 0
        ),
    )


def test_message_identity_is_immutable_and_registration_is_idempotent(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)
        first = session.register_user_message("m1", "用户原文：保留 API")
        before = session.store.events()
        assert session.register_user_message("m1", first["body"]) == first
        assert session.store.events() == before
        assert first["identity"] == session.identity
        for register, text in [
            (session.register_model_message, first["body"]),
            (session.register_user_message, "changed"),
        ]:
            with pytest.raises(ValueError, match="task_message_identity_conflict"):
                register("m1", text)
        assert session.store.events() == before


@pytest.mark.parametrize("damage", ["checksum", "quote", "offset", "cross_task", "model", "role"])
def test_forged_or_unauthorized_sources_cannot_promote_constraints(tmp_path, damage):
    with session_for(tmp_path) as session:
        seed(session)
        refs = source(session, "请保留 API", model=damage == "model")
        update = constraint(refs)
        if damage == "checksum":
            refs[0]["message_checksum"] = "0" * 64
        elif damage == "quote":
            refs[0]["quote"] = "允许修改 API"
        elif damage == "offset":
            refs[0]["end"] += 1
        elif damage == "cross_task":
            other = tmp_path / "other"
            other.mkdir()
            with session_for(other) as other_session:
                refs[:] = source(other_session, "请保留 API")
        elif damage == "role":
            update["source_kind"] = "user"
        before, raw = session.store.events(), session.long_task_state.to_dict()
        with pytest.raises(ValueError):
            submit(session, [update])
        assert session.store.events() == before
        assert session.long_task_state.to_dict() == raw


def test_constraint_replace_revoke_keep_history_and_only_latest_projection(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)
        refs = source(session, "保留新 API；改用旧 API；撤销要求")
        old = submit(session, [constraint(refs)])["records"][0]
        revised = submit(
            session,
            [
                constraint(
                    refs,
                    "Use old API",
                    action="replace",
                    constraint_id=old["constraint_id"],
                    expected_revision=1,
                )
            ],
            "replace",
        )["records"][0]
        assert session.build_required_context("read-0")["hard_constraints"] == [
            "Keep tests",
            "Use old API",
        ]
        submit(
            session,
            [
                {
                    "kind": "constraint",
                    "action": "revoke",
                    "source_refs": refs,
                    "constraint_id": old["constraint_id"],
                    "expected_revision": 2,
                }
            ],
            "revoke",
        )
        history, latest = constraint_history(session.long_task_state.constraint_records)
        assert [r["status"] for r in history] == ["active", "superseded", "superseded", "revoked"]
        assert latest[old["constraint_id"]]["revision"] == 3
        assert session.long_task_state.constraint_records[1] == old
        assert session.long_task_state.constraint_records[2] == revised
        assert session.build_required_context("read-0")["hard_constraints"] == ["Keep tests"]
        original = session.long_task_state.original_request
        with pytest.raises(ValueError, match="context_already_configured"):
            session.configure_long_task("replace objective")
        session.configure_long_task(original, hard_constraints=["Keep tests"])
        assert session.long_task_state.hard_constraints == ["Keep tests"]
        session.verify_checkpoint(session.checkpoint())


@pytest.mark.parametrize("conflict", ["revision", "plan", "item", "target"])
def test_batch_conflicts_never_publish_partial_state(tmp_path, conflict):
    with session_for(tmp_path) as session:
        seed(session)
        refs = source(session, "保留 API")
        updates = [constraint(refs)]
        kwargs = {}
        if conflict == "revision":
            kwargs["revision"] = 0
        elif conflict == "plan":
            kwargs["version"] = 9
        elif conflict == "item":
            updates.append(constraint([{**refs[0], "quote": "forged"}]))
        else:
            updates.append(
                constraint(refs, action="replace", constraint_id="unknown", expected_revision=1)
            )
        before, raw = session.store.events(), session.long_task_state.to_dict()
        with pytest.raises(ValueError):
            submit(session, updates, **kwargs)
        assert session.store.events() == before and session.long_task_state.to_dict() == raw


def test_replay_returns_same_receipt_without_rolling_back_newer_state(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)
        update = constraint(source(session, "保留 API"))
        revision = session.long_task_state.state_revision
        receipt = submit(session, [update], revision=revision)
        submit(
            session,
            [constraint(source(session, "Keep docs", message_id="later"), "Keep docs")],
            "later",
        )
        before = session.store.events()
        assert submit(session, [update], revision=revision) == receipt
        assert session.store.events() == before
        assert session.long_task_state.hard_constraints == [
            "Keep tests",
            "Preserve API",
            "Keep docs",
        ]
        with pytest.raises(ValueError, match="context_batch_identity_conflict"):
            submit(session, [constraint(update["source_refs"], "Different")], revision=revision)


def test_user_decisions_use_sources_while_technical_decisions_still_require_evidence(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        seed(session)
        through_analysis(session)
        refs = source(session, "选择保留 API")
        user = {
            "kind": "decision",
            "action": "add",
            "statement": "Keep API",
            "source_refs": refs,
            "node_id": "edit",
        }
        first = submit(session, [user])["records"][0]
        assert first["source_kind"] == "user" and first["evidence_refs"] == []
        model_refs = source(session, "Inspect the implementation", message_id="model", model=True)
        technical = {**user, "source_refs": model_refs}
        before = session.store.events()
        with pytest.raises(ValueError, match="decision_content_or_source_missing"):
            submit(session, [technical], "technical")
        assert session.store.events() == before
        technical["evidence_refs"] = session.plan.node("read-0").output_evidence_refs
        technical_record = submit(session, [technical], "technical")["records"][0]
        assert technical_record["source_kind"] == "technical"
        assert len(session.build_required_context("edit")["decisions"]) == 2
        second = submit(
            session,
            [
                {
                    **user,
                    "action": "replace",
                    "statement": "Keep API v2",
                    "decision_id": first["decision_id"],
                    "expected_revision": 1,
                }
            ],
            "replace",
        )["records"][0]
        # Revocation can retire a technical choice whose original evidence is now stale.
        (tmp_path / "value.py").write_text("value = 9\n")
        submit(
            session,
            [
                {
                    "kind": "decision",
                    "action": "revoke",
                    "node_id": "edit",
                    "decision_id": technical_record["decision_id"],
                    "expected_revision": 1,
                    "source_refs": model_refs,
                },
                {
                    "kind": "decision",
                    "action": "revoke",
                    "node_id": "edit",
                    "decision_id": first["decision_id"],
                    "expected_revision": 2,
                    "source_refs": refs,
                },
            ],
            "revoke",
        )
        history, latest = decision_history(session.long_task_state.key_decisions)
        assert all(r["status"] == "revoked" for r in latest.values())
        assert second["revision"] == 2 and len(history) == 5
        assert not session.build_long_task_context("edit")["key_decisions"]


def test_workers_and_expired_owner_cannot_register_or_commit(tmp_path, monkeypatch):
    with session_for(tmp_path) as session:
        seed(session)
        update = constraint(source(session, "保留 API"))
        errors = []

        def worker():
            for call in [
                lambda: session.register_user_message("worker", "Fake user"),
                lambda: submit(session, [update]),
            ]:
                try:
                    call()
                except ValueError as exc:
                    errors.append(str(exc))

        before = session.store.events()
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert errors == ["owner_thread_required"] * 2

        def expired(**kwargs):
            raise ValueError("owner_generation_expired")

        monkeypatch.setattr(session, "assert_execution_allowed", expired)
        with pytest.raises(ValueError, match="owner_generation_expired"):
            submit(session, [update])
        assert session.store.events() == before


@pytest.mark.parametrize("node_id", ["edit", "verify"])
def test_context_changes_preserve_confirmed_effect_receipt(tmp_path, node_id):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        seed(session)
        through_analysis(session)
        session.run_node("edit", lambda attempt: write(session, attempt))
        if node_id == "verify":
            session.run_node(
                "verify",
                lambda a: session.verify_result(
                    a,
                    {"all_passed": True, "total_tests": 1},
                    {"command": ["pytest"], "completed": True},
                ),
            )
        evidence = deepcopy(
            session.evidence.get(session.plan.node(node_id).output_evidence_refs[0])
        )
        before = deepcopy(session.store.latest("operation", "operation_id"))
        submit(session, [constraint(source(session, "Keep docs"), "Keep docs")])
        context = session.build_required_context(node_id)
        assert context["evidence_phase"] == "after_effect"
        assert session.plan.node(node_id).status == "succeeded"
        assert session.evidence.get(evidence["evidence_id"]) == evidence
        assert session.store.latest("operation", "operation_id") == before
        assert (tmp_path / "value.py").read_text() == "value = 2\n"


@pytest.mark.parametrize("point", ["before_context_commit", "context_committed"])
def test_crash_windows_restore_atomic_batch_and_replay_once(tmp_path, point):
    with session_for(tmp_path) as session:
        seed(session)
        update = constraint(source(session, "保留 API"))
        revision = session.long_task_state.state_revision
    code = """
import os, sys
from tests.plan_support import session_for
from tests.test_context_updates import constraint, submit
with session_for(sys.argv[1]) as session:
    session.fault = lambda p: os._exit(78) if p == sys.argv[2] else None
    submit(session, [constraint([session.source_reference('change')])])
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), point],
        timeout=30,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.getcwd()},
    )
    assert child.returncode == 78, child.stderr
    with session_for(tmp_path) as restored:
        assert ("Preserve API" in restored.long_task_state.hard_constraints) == (
            point == "context_committed"
        )
        submit(restored, [update], revision=revision)
        before = restored.store.events()
        submit(restored, [update], revision=revision)
        assert restored.store.events() == before
        assert len(restored.long_task_state.constraint_records) == 2
        restored.verify_checkpoint(restored.checkpoint())


def test_commit_ack_loss_blocks_stale_checkpoint_until_replay(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)
        updates = [constraint(source(session, "保留 API"))]
        revision = session.long_task_state.state_revision

        def fault(point):
            if point == "context_committed":
                raise RuntimeError("ack_lost")

        session.fault = fault
        with pytest.raises(RuntimeError, match="ack_lost"):
            submit(session, updates)
        session.fault = None
        with pytest.raises(ValueError, match="context_commit_recovery_required"):
            session.checkpoint()
        with pytest.raises(ValueError, match="context_commit_recovery_required"):
            session.prepare("read-0")
        submit(session, updates, revision=revision)
        assert session.long_task_state.hard_constraints == ["Keep tests", "Preserve API"]


def test_inflight_operation_blocks_updates_without_losing_execution_facts(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        seed(session)
        through_analysis(session)
        update = constraint(source(session, "Keep docs", message_id="inflight"), "Keep docs")
        attempt = session.prepare("edit")
        session.local.attempt = attempt
        agent = type("ToolAgent", (), {"session": {}})()
        flow = session.tool_operation(
            agent, "write_file", {"path": "value.py", "content": "value = 2"}
        )
        next(flow)
        operation = session.operations(attempt["attempt_id"])[-1]
        before, raw = session.store.events(), session.long_task_state.to_dict()
        with pytest.raises(ValueError, match="context_execution_not_quiescent"):
            submit(session, [update])
        assert session.store.events() == before and session.long_task_state.to_dict() == raw
        assert (
            session.store.latest("operation", "operation_id")[operation["operation_id"]]["phase"]
            == "dispatched"
        )
        assert (tmp_path / "value.py").read_text() == "value = 1\n"
        flow.close()


def test_owner_expiring_during_evidence_validation_cannot_publish_batch(tmp_path, monkeypatch):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        seed(session)
        through_analysis(session)
        refs = source(session, "Use this implementation", model=True)
        update = {
            "kind": "decision",
            "action": "add",
            "statement": "Use implementation",
            "node_id": "edit",
            "source_refs": refs,
            "evidence_refs": session.plan.node("read-0").output_evidence_refs,
        }
        inspect, expired = session.evidence.inspect, []

        def check(ref, **kwargs):
            result = inspect(ref, **kwargs)
            expired.append(True)
            return result

        def fence(**kwargs):
            if expired:
                raise ValueError("owner_generation_expired")

        monkeypatch.setattr(session.evidence, "inspect", check)
        monkeypatch.setattr(session, "assert_execution_allowed", fence)
        before, raw = session.store.events(), session.long_task_state.to_dict()
        with pytest.raises(ValueError, match="owner_generation_expired"):
            submit(session, [update])
        assert session.store.events() == before and session.long_task_state.to_dict() == raw


def test_journal_multi_append_rolls_back_all_entries_on_serialization_failure(tmp_path):
    with session_for(tmp_path) as session:
        before = session.store.events()
        with pytest.raises(TypeError):
            session.store.append_many([("first", {"id": "a"}), ("bad", {"value": object()})])
        assert session.store.events() == before


def test_resealed_source_identity_is_rejected_on_resume(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)
        record = session.task_messages.records()["original-request"]
        record["identity"]["task_id"] = "different-task"
        record["checksum"] = digest({k: v for k, v in record.items() if k != "checksum"})
        session.store.append("task_message", record)
    with pytest.raises(ValueError, match="task_message_integrity_invalid"):
        session_for(tmp_path)
