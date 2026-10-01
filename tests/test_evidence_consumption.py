"""Current-node explanations stay distinct from proof and body delivery."""

import json

import pytest

from agent_runtime.checkpoint import create_checkpoint, evaluate_resume_state
from agent_runtime.context_manager import ContextManager
from agent_runtime.errors import ContextBuildBlockedError
from agent_runtime.plan_runtime.models import digest
from agent_runtime.task_state import TaskState
from tests.plan_support import read, session_for, simple_plan, through_analysis, write
from tests.test_required_long_task_context import _agent


def _rewrite(session, ref, **fields):
    record = {**session.evidence.get(ref), **fields}
    record.pop("checksum")
    record["checksum"] = digest(record)
    session.store.append("evidence", record)
    return record


@pytest.mark.parametrize(
    "damage,status,reason",
    [
        ("missing", "invalid", "evidence_missing"),
        ("checksum", "invalid", "evidence_checksum_mismatch"),
        ("scope", "invalid", "evidence_scope_mismatch"),
        ("source", "stale", "source_changed"),
        ("blob", "invalid", "blob_unavailable"),
        ("versions", "unknown", "file_versions_missing"),
        ("io", "unknown", "workspace_unverifiable"),
    ],
)
def test_inspection_explains_rejection_without_promoting_unknown(
    tmp_path, monkeypatch, damage, status, reason
):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.run_node("read-0", lambda attempt: read(session, attempt))
        ref = session.plan.node("read-0").output_evidence_refs[0]
        record = session.evidence.get(ref)
        if damage == "missing":
            ref = "E-missing"
        elif damage == "checksum":
            session.store.append("evidence", {**record, "checksum": "wrong"})
        elif damage == "scope":
            _rewrite(session, ref, identity={**session.identity, "run_id": "other"})
        elif damage == "source":
            (tmp_path / "value.py").write_text("value = 9\n")
        elif damage == "blob":
            (session.store.root / "blobs" / record["blob_ref"]).write_text("corrupt")
        elif damage == "versions":
            _rewrite(session, ref, file_versions=None)
        else:

            def unavailable(_root):
                raise OSError("source inaccessible")

            monkeypatch.setattr("agent_runtime.plan_runtime.evidence.snapshot", unavailable)
        before = session.store.events()
        inspection = session.evidence.inspect(ref)
        assert inspection["status"] == status and inspection["reason"] == reason
        assert not session.evidence.valid(ref)
        assert session.store.events() == before


def test_analysis_dependency_rejection_keeps_the_actual_cause(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        through_analysis(session)
        ref = session.plan.node("analysis").output_evidence_refs[0]
        _rewrite(session, ref, input_refs=["E-missing"])
        check = session.evidence.inspect(ref)
        assert check["status"] == "invalid"
        assert check["reason"] == "dependency_unusable:evidence_missing"
        assert check["failed_dependency"] == "E-missing"


def test_receipt_after_patch_uses_historical_inputs_without_replaying(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)
        session.run_node("edit", lambda attempt: write(session, attempt))
        context = session.build_required_context()
        summary = context["evidence_summaries"][0]
        check = context["evidence_checks"][0]
        assert summary["kind"] == "patch_applied"
        assert summary["changed_paths"] == ["value.py"]
        assert summary["receipt"]["status"] == "success"
        assert "not permission to replay" in summary["supports"]
        assert all(item["use"] == "historical" for item in check["dependencies"])
        read_ref = session.plan.node("read-0").output_evidence_refs[0]
        assert session.evidence.inspect(read_ref)["status"] == "stale"
        assert session.evidence.inspect(read_ref, historical=True)["status"] == "valid"
        assert (tmp_path / "value.py").read_text() == "value = 2\n"
        assert (
            len(
                [
                    op
                    for op in session.store.latest("operation", "operation_id").values()
                    if op["effect"] == "write"
                ]
            )
            == 1
        )


@pytest.mark.parametrize("native", [False, True])
def test_required_summary_reaches_actual_request_even_when_optional_bodies_are_empty(
    tmp_path, native, monkeypatch
):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("original goal", hard_constraints=["preserve tests"])
        through_analysis(session)
        agent, client = _agent(tmp_path, session, native=native)
        for name in ("_get_workspace", "_get_memory"):
            monkeypatch.setattr(ContextManager, name, lambda self: "")
        monkeypatch.setattr(ContextManager, "_get_knowledge", lambda self, query: "")
        monkeypatch.setattr(ContextManager, "_get_compressed_history", lambda self, metadata: "")
        assert agent.ask("continue", skip_plan=True) == "done"
        text = json.dumps(client.requests[0].messages) if native else client.prompts[0]
        assert "conclusion_preview" in text and "value is incorrect" in text
        assert "semantic correctness is not certified" in text
        summary = agent.session["long_task_context"]["evidence_summaries"][0]
        check = agent.session["long_task_context"]["evidence_checks"][0]
        assert summary["record_checksum_prefix"] == check["record_checksum"][:12]
        records = agent.session["context_manifest"]["evidence_consumption"]
        primary = next(item for item in records if item["required"])
        assert primary["summary_in_request"] and not primary["body_in_request"]
        assert all(item["status"] == "valid" for item in records)
        assert any(item["kind"] == "observation_present" for item in records)
        assert all(
            item["body_reason"] == "not_verified"
            for item in records
            if item["kind"] == "observation_present"
        )
        checkpoint = create_checkpoint(agent, TaskState.create(user_request="continue"), "continue")
        assert checkpoint["context_manifest"]["evidence_consumption"] == records
        records[0]["body_forms"].append("modified after checkpoint")
        assert checkpoint["context_manifest"]["evidence_consumption"][0]["body_forms"] == []
        assert evaluate_resume_state(agent)["status"] == "full-valid"


@pytest.mark.parametrize("native", [False, True])
def test_stale_required_evidence_returns_reason_and_zero_model_calls(tmp_path, native):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)
        (tmp_path / "value.py").write_text("external = 9\n")
        agent, client = _agent(tmp_path, session, native=native)
        assert "needs_retrieval" in agent.ask("continue", skip_plan=True)
        check = agent.session["context_blocked"]["evidence_checks"][0]
        assert check["status"] == "stale" and check["reason"] == "source_changed"
        assert not client.prompts
        if native:
            assert not client.requests


def test_long_fields_are_explicit_previews_without_changing_durable_conclusion(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)
        ref = session.plan.node("analysis").output_evidence_refs[0]
        original = "analysis detail " * 2000
        _rewrite(session, ref, conclusion=original)
        before = session.store.events()
        summary = session.build_required_context()["evidence_summaries"][0]
        assert summary["conclusion_preview"] == original[:512]
        assert summary["omitted_fields"] == ["conclusion"]
        assert session.evidence.get(ref)["conclusion"] == original
        assert session.store.events() == before


def test_verification_explanation_is_bound_to_tested_versions(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)
        session.run_node("edit", lambda attempt: write(session, attempt))
        session.run_node(
            "verify",
            lambda attempt: session.verify_result(
                attempt,
                {"all_passed": True, "total_tests": 1},
                {"command": ["pytest", "test_value.py"], "completed": True},
            ),
        )
        context = session.build_required_context()
        summary = context["evidence_summaries"][0]
        ref = context["evidence_refs"][0]
        assert summary["command_preview"] == ["pytest", "test_value.py"]
        assert summary["receipt"]["total_tests"] == 1
        assert "not exhaustive coverage" in summary["supports"]
        (tmp_path / "value.py").write_text("value = 3\n")
        assert session.evidence.inspect(ref)["status"] == "stale"


@pytest.mark.parametrize("native", [False, True])
def test_required_summaries_do_not_bypass_total_input_budget(tmp_path, native):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)
        agent, client = _agent(tmp_path, session, native=native, budget=512)
        assert "context_required_over_budget" in agent.ask("continue", skip_plan=True)
        assert not client.prompts
        assert agent.config.prompt_budget == 512


def test_projection_failure_does_not_silently_drop_an_evidence_explanation(tmp_path, monkeypatch):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)

        def fail(*args):
            raise ValueError("malformed evidence explanation")

        monkeypatch.setattr("agent_runtime.plan_runtime.evidence_view.evidence_summary", fail)
        agent, client = _agent(tmp_path, session)
        with pytest.raises(ContextBuildBlockedError, match="state_mismatch"):
            ContextManager(agent).prepare_request("continue", protocol="xml")
        assert not client.prompts


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("large", [False, True])
def test_actual_request_distinguishes_checked_source_summary_and_retained_body(
    tmp_path, native, large, monkeypatch
):
    from agent_runtime.code_exploration.io import read_file_result

    # A single long line is already clipped by the read tool's line bound.
    # Multiple bounded lines exercise optional-context omission instead.
    body = ("RAW_WORD " * 50 + "\n") * 60 if large else "value = 1\n"
    (tmp_path / "value.py").write_text(body)
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("inspect source", hard_constraints=["do not edit"])
        agent, client = _agent(tmp_path, session, native=native, budget=4500 if large else 6000)
        agent.shared_run_id = session.identity["run_id"]
        agent._l2_agent = "observer"
        agent._l2_task_id = session.identity["task_id"]
        agent.session["id"] = session.identity["session_id"]
        agent.session["session_identity"] = dict(session.identity)
        agent.session["session_scope"] = {"session_id": session.identity["session_id"]}

        def invoke(attempt):
            result = read_file_result(agent.tool_context, {"path": "value.py"})
            result.metadata["retrieval_result"].update(
                completeness="partial", truncation_reasons=["hit_limit"]
            )
            session.execute_tool(agent, "read_file", {"path": "value.py"}, lambda: result)
            return session.tool_result(attempt)

        assert session.run_node("read-0", invoke)["status"] == "success"
        ref = session.plan.node("read-0").output_evidence_refs[0]
        oid = session.evidence.get(ref)["observation_id"]
        agent.record(
            {
                "role": "tool",
                "content": "discard this unchecked body",
                "tool_name": "read_file",
                "observation_id": oid,
            }
        )
        monkeypatch.setattr(ContextManager, "_get_compressed_history", lambda self, metadata: "")
        assert agent.ask("continue", skip_plan=True) == "done"
        text = json.dumps(client.requests[0].messages) if native else client.prompts[0]
        assert "discard this unchecked body" not in text
        assert "partial" in text and "hit_limit" in text
        record = next(
            item
            for item in agent.session["context_manifest"]["evidence_consumption"]
            if item["evidence_ref"] == ref
        )
        assert record["status"] == "valid" and record["summary_in_request"]
        assert record["body_in_request"] is (not large)
        if large:
            assert "RAW_WORD" not in text and record["body_reason"] == "not_selected"
        else:
            assert "value = 1" in text and record["body_forms"] == ["tool_feedback"]


@pytest.mark.parametrize(
    "kind,reason", [("patch", "patch_receipt_invalid"), ("verify", "verification_receipt_invalid")]
)
def test_invalid_receipt_cannot_be_consumed_as_success(tmp_path, kind, reason):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        through_analysis(session)
        session.run_node("edit", lambda attempt: write(session, attempt))
        if kind == "verify":
            session.run_node(
                "verify",
                lambda attempt: session.verify_result(
                    attempt,
                    {"all_passed": True, "total_tests": 1},
                    {"command": ["pytest"], "completed": True},
                ),
            )
        node = session.plan.node("edit" if kind == "patch" else "verify")
        ref = next(
            ref
            for ref in node.output_evidence_refs
            if session.evidence.get(ref)["kind"] != "observation_present"
        )
        _rewrite(session, ref, receipt={})
        result = session.evidence.inspect(ref)
        assert result["status"] == "invalid" and result["reason"] == reason
        assert not session.evidence.valid(ref)


def test_dependency_cycle_is_rejected_with_a_stable_reason(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        through_analysis(session)
        ref = session.plan.node("analysis").output_evidence_refs[0]
        _rewrite(session, ref, input_refs=[ref])
        check = session.evidence.inspect(ref)
        assert check["status"] == "invalid"
        assert check["reason"] == "dependency_unusable:evidence_cycle"
        assert not session.evidence.valid(ref)


def test_manifest_does_not_label_historical_dependency_as_current_summary(tmp_path):
    from agent_runtime.plan_runtime.evidence_view import consumption_manifest, evidence_summary

    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)
        session.run_node("edit", lambda attempt: write(session, attempt))
        context = session.build_required_context()
        dependency = context["evidence_checks"][0]["dependencies"][0]
        ref = dependency["evidence_ref"]
        check = session.evidence.inspect(ref)
        assert check["status"] == "valid" and dependency["use"] == "historical"
        # Explicit consumers may require the same archived observation both
        # directly and as a receipt dependency. Both checks use real records.
        context["evidence_refs"].append(ref)
        context["evidence_checks"].append(check)
        context["evidence_summaries"].append(evidence_summary(session.evidence.get(ref), check))
        records = consumption_manifest(
            context, {"request_hash": "prepared-input"}, {"state": json.dumps(context)}
        )
        current = next(
            item
            for item in records
            if item["kind"] == "observation_present" and item["use"] == "current"
        )
        historical = next(
            item
            for item in records
            if item["evidence_ref"] == current["evidence_ref"] and item["use"] == "historical"
        )
        assert current["summary_in_request"] and current["required"]
        assert not historical["summary_in_request"] and not historical["required"]
        assert historical["status"] == "valid"


@pytest.mark.parametrize("omit_tail", [False, True])
def test_native_consumption_tracks_retained_groups_after_forced_action(tmp_path, omit_tail):
    from agent_runtime.code_exploration.io import read_file_result
    from tests.test_context_assembly import _group

    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("inspect source")
        agent, client = _agent(tmp_path, session, native=True)
        agent.shared_run_id = session.identity["run_id"]
        agent._l2_agent = "observer"
        agent._l2_task_id = session.identity["task_id"]
        agent.session["id"] = session.identity["session_id"]
        agent.session["session_identity"] = dict(session.identity)
        agent.session["session_scope"] = {"session_id": session.identity["session_id"]}

        def invoke(attempt):
            result = read_file_result(agent.tool_context, {"path": "value.py"})
            session.execute_tool(agent, "read_file", {"path": "value.py"}, lambda: result)
            return session.tool_result(attempt)

        session.run_node("read-0", invoke)
        ref = session.plan.node("read-0").output_evidence_refs[0]
        oid = session.evidence.get(ref)["observation_id"]
        request, _ = ContextManager(agent).prepare_request(
            "continue",
            protocol="native",
            native_tail=_group("source-call", "unchecked body"),
            tail_refs={"source-call": oid},
            action_required=omit_tail,
        )
        client.complete_turn(request)
        text = json.dumps(client.requests[0].messages)
        assert "unchecked body" not in text
        record = next(
            item
            for item in agent.session["context_manifest"]["evidence_consumption"]
            if item["evidence_ref"] == ref
        )
        assert record["status"] == "valid" and record["summary_in_request"]
        assert record["body_in_request"] is (not omit_tail)
        assert record["body_forms"] == ([] if omit_tail else ["native_tool_result"])
        assert ("value = 1" in text) is (not omit_tail)
