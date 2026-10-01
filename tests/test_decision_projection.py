"""Decision revisions survive real requests, source changes and recovery."""

import json
import os
import subprocess
import sys
import threading
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_runtime.checkpoint import create_checkpoint, evaluate_resume_state
from agent_runtime.context_manager import ContextManager
from agent_runtime.errors import ContextBuildBlockedError
from agent_runtime.plan_runtime.decisions import decision_history, project_decisions
from agent_runtime.plan_runtime.models import digest
from agent_runtime.plan_runtime.recovery import recover
from agent_runtime.plan_runtime.replan import replan
from agent_runtime.task_state import TaskState
from tests.plan_support import read, session_for, simple_plan, through_analysis, write
from tests.test_required_long_task_context import _agent


def _record(session, statement="FIRST_CHOICE", *, node_id="edit", refs=None, **kwargs):
    refs = refs if refs is not None else session.plan.node("read-0").output_evidence_refs
    return session.record_decision(
        statement,
        node_id=node_id,
        evidence_refs=refs,
        expected_plan_version=session.plan.plan_version,
        source="owner:explicit-choice",
        **kwargs,
    )


def _replace(session, previous, statement="REVISED_CHOICE", **kwargs):
    return session.replace_decision(
        previous["decision_id"],
        previous["revision"],
        statement,
        node_id=previous["node_id"],
        evidence_refs=previous["evidence_refs"],
        expected_plan_version=session.plan.plan_version,
        source="owner:review",
        **kwargs,
    )


@pytest.mark.parametrize("native", [False, True])
def test_only_latest_decision_reaches_actual_request_and_checkpoint(tmp_path, native):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value", hard_constraints=["preserve tests"])
        through_analysis(session)
        old = _record(session)
        new = _replace(session, old, rationale="explicit owner reconsideration")
        history, latest = decision_history(session.long_task_state.key_decisions)
        assert [item["status"] for item in history] == ["superseded", "active"]
        assert latest[old["decision_id"]]["revision"] == 2
        assert session.long_task_state.key_decisions[0] == old
        agent, client = _agent(tmp_path, session, native=native)
        assert agent.ask("continue", skip_plan=True) == "done"
        text = json.dumps(client.requests[0].messages) if native else client.prompts[0]
        assert "REVISED_CHOICE" in text and "FIRST_CHOICE" not in text
        assert "preserve tests" in text
        consumed = agent.session["context_manifest"]["evidence_consumption"]
        assert all(
            any(
                item["evidence_ref"] == ref and item["required"] and item["summary_in_request"]
                for item in consumed
            )
            for ref in new["evidence_refs"]
        )
        refs = agent.session["context_manifest"]["decision_refs"]
        assert refs == [
            {
                "decision_id": new["decision_id"],
                "revision": 2,
                "checksum": new["checksum"],
                "status": "active",
            }
        ]
        cp = create_checkpoint(agent, TaskState.create(user_request="continue"), "continue")
        agent.session["long_task_context"]["decisions"][0]["statement"] = "CORRUPT_CACHE"
        agent.session["long_task_context"]["decisions"][0]["evidence_refs"].clear()
        refs[0]["revision"] = 999
        assert cp["long_task_context"]["decisions"][0]["statement"] == "REVISED_CHOICE"
        assert cp["long_task_context"]["decisions"][0]["evidence_refs"]
        assert cp["context_manifest"]["decision_refs"][0]["revision"] == 2


@pytest.mark.parametrize("native", [False, True])
def test_required_decision_review_blocks_without_writing_state(tmp_path, native):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session, reads=2))
        session.configure_long_task("inspect source")
        session.run_node("read-0", lambda attempt: read(session, attempt))
        record = _record(session, node_id="read-1")
        (tmp_path / "value.py").write_text("value = 9\n")
        before = session.store.events()
        agent, client = _agent(tmp_path, session, native=native)
        with pytest.raises(ContextBuildBlockedError, match="decision_needs_review"):
            ContextManager(agent).prepare_request(
                "continue", protocol="native" if native else "xml"
            )
        assert session.store.events() == before
        assert "decision_needs_review" in agent.ask("continue", skip_plan=True)
        check = agent.session["context_blocked"]["decision_checks"][0]
        assert check["decision_id"] == record["decision_id"]
        assert check["reason"] == "evidence_unusable:source_changed"
        assert not client.prompts
        if native:
            assert not client.requests
        assert session.long_task_state.key_decisions[0]["status"] == "active"


@pytest.mark.parametrize("native", [False, True])
def test_unrelated_stale_decision_does_not_block_exploration(tmp_path, native):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session, reads=2))
        session.configure_long_task("inspect source")
        session.run_node("read-0", lambda attempt: read(session, attempt))
        _record(session, "UNRELATED_CHOICE")
        (tmp_path / "value.py").write_text("value = 9\n")
        agent, client = _agent(tmp_path, session, native=native)
        assert agent.ask("continue", skip_plan=True) == "done"
        text = json.dumps(client.requests[0].messages) if native else client.prompts[0]
        assert "UNRELATED_CHOICE" not in text
        assert agent.session["context_manifest"]["decision_refs"] == []


def test_refreshing_evidence_never_reactivates_or_rebinds_old_decision(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session, reads=2))
        session.configure_long_task("inspect source")
        session.run_node("read-0", lambda attempt: read(session, attempt))
        old = _record(session)
        (tmp_path / "value.py").write_text("value = 9\n")
        fresh = session.refresh_observation_with_node(
            old["evidence_refs"][0], "read-1", lambda attempt: read(session, attempt)
        )
        projected = project_decisions(
            session.long_task_state.key_decisions, session.plan, session.evidence, "edit"
        )
        assert not projected["active"] and projected["checks"][0]["status"] == "needs_review"
        assert session.long_task_state.key_decisions[0] == old
        new = session.replace_decision(
            old["decision_id"],
            1,
            "REVIEWED_FOR_NEW_SOURCE",
            source="owner:explicit-review",
            evidence_refs=[fresh],
            node_id="edit",
            expected_plan_version=session.plan.plan_version,
        )
        assert new["revision"] == 2 and new["evidence_refs"] == [fresh]
        assert (
            project_decisions(
                session.long_task_state.key_decisions, session.plan, session.evidence, "edit"
            )["active"][0]["revision"]
            == 2
        )


def test_plan_revision_requires_explicit_decision_review(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)
        old = _record(session)
        replan(
            session,
            simple_plan(session),
            reason="explicit reconsideration",
            evidence_refs=old["evidence_refs"],
        )
        with pytest.raises(ContextBuildBlockedError, match="decision_needs_review") as exc:
            session.build_required_context("edit")
        assert exc.value.metadata["decision_checks"][0]["reason"] == "plan_version_changed"
        new = _replace(session, old)
        assert new["plan_version"] == 2
        assert session.build_required_context("edit")["decisions"][0]["revision"] == 2


@pytest.mark.parametrize(
    "damage", ["duplicate", "revision", "version", "scope", "empty", "evidence"]
)
def test_invalid_or_late_replacements_do_not_change_durable_state(tmp_path, damage):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)
        old = _record(session)
        if damage == "duplicate":
            _replace(session, old)
        kwargs = dict(
            node_id="edit",
            evidence_refs=old["evidence_refs"],
            expected_plan_version=1,
            source="owner:replacement",
        )
        revision = 1
        if damage == "revision":
            revision = 9
        elif damage == "version":
            kwargs["expected_plan_version"] = 0
        elif damage == "scope":
            kwargs["node_id"] = "verify"
        elif damage == "empty":
            kwargs["source"] = ""
        elif damage == "evidence":
            kwargs["evidence_refs"] = ["E-missing"]
        before = session.store.events()
        with pytest.raises(ValueError):
            session.replace_decision(old["decision_id"], revision, "LATE_CHOICE", **kwargs)
        assert session.store.events() == before


def test_worker_cannot_promote_a_decision(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        through_analysis(session)
        errors = []

        def worker():
            try:
                _record(session)
            except ValueError as exc:
                errors.append(str(exc))

        before = session.store.events()
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert errors == ["owner_thread_required"] and session.store.events() == before


@pytest.mark.parametrize("native", [False, True])
def test_confirmed_patch_ignores_stale_preimage_decision_without_replaying(tmp_path, native):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)
        _record(session, "PREIMAGE_CHOICE")
        agent, client = _agent(tmp_path, session, native=native)

        def edit(attempt):
            write(session, attempt)
            assert agent.ask("continue", skip_plan=True) == "done"
            context = agent.session["long_task_context"]
            assert context["evidence_phase"] == "after_effect"
            assert not context["decisions"]
            assert context["decision_checks"][0]["status"] == "needs_review"
            return session.tool_result(attempt)

        assert session.run_node("edit", edit)["status"] == "success"
        text = json.dumps(client.requests[0].messages) if native else client.prompts[0]
        assert "PREIMAGE_CHOICE" not in text
        writes = [
            op
            for op in session.store.latest("operation", "operation_id").values()
            if op["effect"] == "write"
        ]
        assert len(writes) == 1 and (tmp_path / "value.py").read_text() == "value = 2\n"


def test_resume_rebuilds_latest_decision_after_checkpoint_and_ignores_cache(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)
        old = _record(session)
        agent, _ = _agent(tmp_path, session)
        ContextManager(agent).prepare_request("continue", protocol="xml")
        create_checkpoint(agent, TaskState.create(user_request="continue"), "continue")
        _replace(session, old)
    with session_for(tmp_path) as restored:
        agent._plan_session = restored
        agent.session["long_task_context"] = {"decisions": [{"statement": "WRONG_CACHE"}]}
        result = evaluate_resume_state(agent)
        assert result["status"] == "full-valid", result
        assert agent.session["long_task_context"]["decisions"][0]["revision"] == 2
        assert agent.ask("continue", skip_plan=True) == "done"
        assert "REVISED_CHOICE" in agent.model_client.prompts[0]
        assert "FIRST_CHOICE" not in agent.model_client.prompts[0]


def test_resume_review_reason_clears_old_active_projection(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session, reads=2))
        session.configure_long_task("inspect source")
        session.run_node("read-0", lambda attempt: read(session, attempt))
        _record(session, node_id="read-1")
        agent, _ = _agent(tmp_path, session)
        ContextManager(agent).prepare_request("continue", protocol="xml")
        create_checkpoint(agent, TaskState.create(user_request="continue"), "continue")
        (tmp_path / "value.py").write_text("value = 9\n")
        before = session.store.events()
        result = evaluate_resume_state(agent)
        assert result["status"] == "partial-stale"
        assert result["context_resume_reason"] == "decision_needs_review"
        assert result["context_resume_details"]["decision_checks"][0]["status"] == "needs_review"
        assert "long_task_context" not in agent.session
        assert session.store.events() == before


def test_forged_but_resealed_task_snapshot_cannot_override_journal_prefix(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)
        _record(session)
        seal = deepcopy(session.checkpoint())
        record = seal["long_task_state"]["key_decisions"][0]
        record["statement"] = "FORGED_DECISION"
        record["checksum"] = digest(
            {key: value for key, value in record.items() if key != "checksum"}
        )
        state = seal["long_task_state"]
        state["state_checksum"] = digest(
            {key: value for key, value in state.items() if key != "state_checksum"}
        )
        seal["checksum"] = digest({key: value for key, value in seal.items() if key != "checksum"})
        with pytest.raises(ValueError, match="checkpoint_long_task_journal_mismatch"):
            session.verify_long_task_checkpoint(seal)


@pytest.mark.parametrize("native", [False, True])
def test_callback_cannot_send_a_superseded_decision(tmp_path, native):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)
        old = _record(session)
        agent, client = _agent(tmp_path, session, native=native)
        callback = SimpleNamespace(on_pre_model=lambda **kwargs: _replace(session, old))
        assert "state_mismatch" in agent.ask("continue", callback=callback, skip_plan=True)
        assert not client.prompts
        if native:
            assert not client.requests


def test_overlong_required_decision_is_blocked_without_silent_truncation(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)
        record = _record(session, "required choice " * 10000)
        agent, client = _agent(tmp_path, session)
        assert "context_required_over_budget" in agent.ask("continue", skip_plan=True)
        assert not client.prompts
        assert session.long_task_state.key_decisions[0]["statement"] == record["statement"]


@pytest.mark.parametrize("damage", ["checksum", "chain"])
def test_corrupt_decision_cannot_reach_model_even_in_resealed_task_state(tmp_path, damage):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)
        _record(session)
        record = session.long_task_state.key_decisions[0]
        if damage == "checksum":
            record["statement"] = "UNCHECKED_CHANGE"
        else:
            record["supersedes_ref"] = {"decision_id": record["decision_id"], "revision": 1}
            record["checksum"] = digest(
                {key: value for key, value in record.items() if key != "checksum"}
            )
        session._persist_long_task_state()
        agent, client = _agent(tmp_path, session)
        assert "state_mismatch" in agent.ask("continue", skip_plan=True)
        assert not client.prompts


def test_expired_owner_cannot_append_decision(tmp_path, monkeypatch):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        through_analysis(session)
        before = session.store.events()

        def reject(**kwargs):
            raise ValueError("owner_generation_expired")

        monkeypatch.setattr(session, "_fence", reject)
        with pytest.raises(ValueError, match="owner_generation_expired"):
            _record(session)
        assert session.store.events() == before


def test_dispatched_operation_prevents_decision_changes(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        through_analysis(session)
        attempt = session.prepare("edit")
        session.local.attempt = attempt

        def interrupt(point):
            if point == "tool_dispatched":
                raise RuntimeError("interrupt before raw write")

        session.fault = interrupt
        with pytest.raises(RuntimeError, match="interrupt before raw write"):
            write(session, attempt)
        session.fault = None
        before = session.store.events()
        with pytest.raises(ValueError, match="decision_execution_not_quiescent"):
            _record(session)
        assert session.store.events() == before
        assert (tmp_path / "value.py").read_text() == "value = 1\n"


def test_process_exit_after_decision_commit_restores_one_revision(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    code = """
import os, sys
from tests.plan_support import session_for, simple_plan, through_analysis
with session_for(sys.argv[1]) as session:
    session.create(simple_plan(session))
    session.configure_long_task('fix value')
    through_analysis(session)
    session.fault = lambda point: os._exit(78) if point == 'decision_recorded' else None
    session.record_decision('DURABLE_CHOICE', source='owner:explicit', node_id='edit',
                            evidence_refs=session.plan.node('read-0').output_evidence_refs,
                            expected_plan_version=1)
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        timeout=30,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.getcwd()},
    )
    assert child.returncode == 78, child.stderr
    with session_for(tmp_path) as restored:
        assert not recover(restored)["uncertain"]
        history, latest = decision_history(restored.long_task_state.key_decisions)
        assert len(history) == len(latest) == 1 and history[0]["revision"] == 1
        agent, client = _agent(tmp_path, restored)
        assert agent.ask("continue", skip_plan=True) == "done"
        assert "DURABLE_CHOICE" in client.prompts[0]


def test_owner_expiring_during_evidence_check_cannot_commit(tmp_path, monkeypatch):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        through_analysis(session)
        before = session.store.events()
        expired = []
        inspect = session.evidence.inspect

        def check(ref, **kwargs):
            result = inspect(ref, **kwargs)
            expired.append(True)
            return result

        def fence(**kwargs):
            if expired:
                raise ValueError("owner_generation_expired")

        monkeypatch.setattr(session.evidence, "inspect", check)
        monkeypatch.setattr(session, "_fence", fence)
        with pytest.raises(ValueError, match="owner_generation_expired"):
            _record(session)
        assert session.store.events() == before
        assert not session.long_task_state.key_decisions


def test_decision_pins_input_record_version_even_when_its_predicate_still_passes(tmp_path):
    from tests.test_evidence_consumption import _rewrite

    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)
        ref = session.plan.node("analysis").output_evidence_refs[0]
        old = _record(session, refs=[ref])
        _rewrite(session, ref, conclusion="a changed recorded conclusion")
        assert session.evidence.valid(ref)
        with pytest.raises(ContextBuildBlockedError, match="decision_needs_review") as exc:
            session.build_required_context("edit")
        check = exc.value.metadata["decision_checks"][0]
        assert check["reason"] == "evidence_record_changed"
        assert check["expected_evidence_checksums"][ref] == old["evidence_checksums"][ref]
        _replace(session, old)
        assert session.build_required_context("edit")["decisions"][0]["revision"] == 2
