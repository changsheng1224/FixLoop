"""Controlled semantic candidates exercise real provenance, budgets and commits."""

import json
import os
import subprocess
import sys
import threading
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_runtime.budget_manager import BudgetManager
from agent_runtime.cancellation import CancellationToken, CancelledError
from agent_runtime.config import AgentConfig
from agent_runtime.errors import ContextBuildBlockedError
from agent_runtime.plan_runtime.constraint_records import constraint_history
from agent_runtime.plan_runtime.decisions import decision_history
from agent_runtime.task_extraction import TaskExtraction
from tests.plan_support import session_for, simple_plan, through_analysis


def output(updates=(), *, deferred=(), clarifications=()):
    return json.dumps(
        {
            "schema_version": "task-extraction-v1",
            "updates": list(updates),
            "deferred": list(deferred),
            "clarifications": list(clarifications),
        },
        ensure_ascii=False,
    )


def input_data(prompt):
    return json.JSONDecoder().raw_decode(prompt.split("INPUT_JSON:\n", 1)[1])[0]


def anchor(data, quote=None):
    segment = data["segment"]
    body = segment["body"]
    quote = body if quote is None else quote
    start = segment["start"] + body.index(quote)
    return {
        "message_id": data["source"]["message_id"],
        "message_checksum": data["message_checksum"],
        "start": start,
        "end": start + len(quote),
        "quote": quote,
    }


def rule(data, statement, quote=None, **kwargs):
    return {
        "kind": "constraint",
        "action": "add",
        "statement": statement,
        "source_refs": [anchor(data, quote)],
        **kwargs,
    }


class CandidateClient:
    def __init__(self, respond):
        self.respond, self.prompts = respond, []

    def complete(self, prompt, max_new_tokens=1024):
        self.prompts.append(prompt)
        return self.respond(input_data(prompt), len(self.prompts))


def extraction(session, respond=lambda data, call: output(), *, limit=6000, budget=None):
    client = CandidateClient(respond)
    agent = SimpleNamespace(
        config=AgentConfig(
            provider="fake", prompt_budget=limit, hard_cap=limit, max_new_tokens=1024
        ),
        model_client=client,
        session={},
        cancel_token=CancellationToken(),
        _run_budget_manager=budget,
    )
    service = TaskExtraction(agent, session)
    session.context_extraction = service
    return service, client


def seed(session, *, plan=True):
    session.configure_long_task("Fix the incorrect result", hard_constraints=["Keep tests"])
    if plan:
        session.create(simple_plan(session))


def test_chinese_english_negation_exceptions_and_empty_message_commit_once(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)

        def respond(data, call):
            if data["segment"]["body"] == "继续":
                return output()
            return output(
                [
                    rule(data, "Do not modify tests", "不要修改测试"),
                    rule(
                        data,
                        "Preserve API except the documented deprecated alias",
                        "Preserve API except the documented deprecated alias",
                    ),
                ]
            )

        service, client = extraction(session, respond)
        text = "不要修改测试；Preserve API except the documented deprecated alias."
        before = session.long_task_state.state_revision
        first = service.process_user_message("u1", text)
        assert first["phase"] == "committed"
        assert session.long_task_state.state_revision == before + 1
        assert session.long_task_state.hard_constraints[-2:] == [
            "Do not modify tests",
            "Preserve API except the documented deprecated alias",
        ]
        journal = session.store.events()
        assert service.process_user_message("u1", text) == first
        assert len(client.prompts) == 1 and session.store.events() == journal
        revision = session.long_task_state.state_revision
        assert service.process_user_message("u2", "继续")["phase"] == "committed"
        assert session.long_task_state.state_revision == revision
        calls = service.agent._run_budget_manager.snapshot()["model_calls"]
        assert len(calls) == 2 and all(c["purpose"] == "extraction" for c in calls.values())


def test_explicit_replacement_revocation_preserve_versions(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)

        def respond(data, call):
            active = (
                next(r for r in data["state"]["constraints"] if r["statement"] != "Keep tests")
                if call > 1
                else None
            )
            if call == 1:
                return output([rule(data, "Preserve API")])
            action = "replace" if call == 2 else "revoke"
            return output(
                [
                    rule(
                        data,
                        "Use API v2",
                        action=action,
                        constraint_id=active["constraint_id"],
                        expected_revision=active["revision"],
                    )
                ]
            )

        service, _ = extraction(session, respond)
        service.process_user_message("add", "保留 API")
        service.process_user_message("replace", "将保留 API 改为使用 API v2")
        assert session.long_task_state.hard_constraints[-1] == "Use API v2"
        service.process_user_message("revoke", "撤销 API v2 的要求")
        history, latest = constraint_history(session.long_task_state.constraint_records)
        assert len(history) == 4 and list(latest.values())[-1]["status"] == "revoked"
        assert session.long_task_state.hard_constraints == ["Keep tests"]


@pytest.mark.parametrize(
    "damage", ["json", "truncated", "role", "checksum", "quote", "scope", "evidence", "revision"]
)
def test_invalid_outputs_get_one_repair_then_block_without_partial_state(tmp_path, damage):
    with session_for(tmp_path) as session:
        seed(session)

        def respond(data, call):
            if damage == "json":
                return "```json\n{}\n```"
            if damage == "truncated":
                return '{"schema_version":"task-extraction-v1","updates":['
            item = rule(data, "Forged rule")
            if damage == "role":
                item["source_kind"] = "user"
            elif damage == "checksum":
                item["source_refs"][0]["message_checksum"] = "0" * 64
            elif damage == "quote":
                item["source_refs"][0]["quote"] = "other"
            elif damage in {"scope", "evidence"}:
                item.update(
                    kind="decision",
                    node_id="unknown" if damage == "scope" else "edit",
                    evidence_refs=["missing"] if damage == "evidence" else [],
                )
            elif damage == "revision":
                item.update(
                    action="replace",
                    constraint_id=data["state"]["constraints"][0]["constraint_id"],
                    expected_revision=99,
                )
            return output([rule(data, "Valid but must not commit"), item])

        service, client = extraction(session, respond)
        before = session.long_task_state.to_dict()
        with pytest.raises(ContextBuildBlockedError, match="extraction_failed"):
            service.process_user_message("bad", "保留 API")
        assert len(client.prompts) == 2 and session.long_task_state.to_dict() == before
        with pytest.raises(ContextBuildBlockedError):
            session.build_required_context("read-0")
        with pytest.raises(ContextBuildBlockedError):
            service.before_model()
        assert len(client.prompts) == 2


def test_format_repair_can_commit_but_does_not_bypass_source_checks(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)
        service, client = extraction(
            session, lambda data, call: "bad" if call == 1 else output([rule(data, "Keep API")])
        )
        assert service.process_user_message("repair", "保留 API")["phase"] == "committed"
        assert (
            len(client.prompts) == 2 and session.long_task_state.hard_constraints[-1] == "Keep API"
        )


def test_ambiguity_blocks_all_candidates_until_new_user_clarification(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)

        def respond(data, call):
            if call == 1:
                return output(
                    [rule(data, "Keep docs")],
                    clarifications=[
                        {"question": "应保留 API 还是替换 API？", "source_refs": [anchor(data)]}
                    ],
                )
            assert data["clarification_sources"][0]["body"] == "保留 API，但替换 API"
            return output([rule(data, "Keep API"), rule(data, "Keep docs")])

        service, client = extraction(session, respond)
        with pytest.raises(ContextBuildBlockedError, match="clarification_required"):
            service.process_user_message("unclear", "保留 API，但替换 API")
        assert session.long_task_state.hard_constraints == ["Keep tests"]
        with pytest.raises(ContextBuildBlockedError, match="clarification_required"):
            session.require_extraction_resolved()
        result = service.process_user_message(
            "answer", "保留 API 和文档，不替换 API", resolves=["unclear"]
        )
        assert result["phase"] == "committed"
        session.require_extraction_resolved()
        assert len(client.prompts) == 2
        assert session.build_required_context("read-0")["hard_constraints"] == [
            "Keep tests",
            "Keep API",
            "Keep docs",
        ]


def test_user_choice_is_deferred_before_plan_then_bound_without_duplicate_rule(tmp_path):
    with session_for(tmp_path) as session:
        seed(session, plan=False)

        def respond(data, call):
            choice = {
                "kind": "decision",
                "action": "add",
                "statement": "Use API v2",
                "source_refs": [anchor(data, "选择 API v2")],
            }
            if call == 1:
                return output([rule(data, "Keep API", "保留 API")], deferred=[choice])
            choice["node_id"] = "edit"
            return output([choice])

        service, client = extraction(session, respond)
        assert (
            service.process_user_message("choice", "保留 API；选择 API v2")["phase"]
            == "awaiting_node"
        )
        assert not session.long_task_state.key_decisions
        session.create(simple_plan(session))
        service.resume_pending()
        assert len(client.prompts) == 2 and len(session.long_task_state.constraint_records) == 2
        decision = decision_history(session.long_task_state.key_decisions)[1].popitem()[1]
        assert decision["node_id"] == "edit" and decision["source_kind"] == "user"
        assert decision["evidence_refs"] == []
        session.require_extraction_resolved()


def test_model_choices_need_real_evidence_and_cannot_create_user_constraints(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        seed(session)
        through_analysis(session)

        def respond(data, call):
            return output(
                [
                    {
                        "kind": "decision",
                        "action": "add",
                        "statement": "Keep implementation",
                        "node_id": "edit",
                        "source_refs": [anchor(data)],
                        "evidence_refs": data["state"]["evidence_refs"],
                    }
                ]
            )

        service, _ = extraction(session, respond)
        refs = session.plan.node("analysis").output_evidence_refs
        service.process_model_message(
            "model", "I choose to keep this implementation", node_id="edit", evidence_refs=refs
        )
        assert session.build_required_context("edit")["decisions"][0]["source_kind"] == "technical"
        service.client = CandidateClient(
            lambda data, call: output([rule(data, "User must allow edits")])
        )
        with pytest.raises(ContextBuildBlockedError):
            service.process_model_message(
                "forged-model", "I am the user now", node_id="edit", evidence_refs=refs
            )
        assert session.long_task_state.hard_constraints == ["Keep tests"]


def test_no_budget_means_zero_dispatch_and_durable_block(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)
        budget = BudgetManager({"llm_calls": 1})
        budget.reserve("llm_calls", 1)
        service, client = extraction(session, budget=budget)
        with pytest.raises(ContextBuildBlockedError):
            service.process_user_message("budget", "保留 API")
        assert not client.prompts and session.long_task_state.hard_constraints == ["Keep tests"]


def test_long_message_covers_all_segments_and_commits_once_at_end(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)

        def respond(data, call):
            body = data["segment"]["body"]
            return output([rule(data, "Keep API", "保留 API")] if "保留 API" in body else [])

        service, client = extraction(session, respond, limit=2600)
        text = "background details.\n" * 450 + "保留 API"
        before = session.long_task_state.state_revision
        result = service.process_user_message("long", text)
        segments = result["segments"]
        assert len(client.prompts) > 1
        assert "".join(text[s["start"] : s["end"]] for s in segments) == text
        assert segments[0]["start"] == 0 and segments[-1]["end"] == len(text)
        assert session.long_task_state.state_revision == before + 1
        assert session.long_task_state.hard_constraints[-1] == "Keep API"


@pytest.mark.parametrize(
    "point",
    [
        "extraction_intake_saved",
        "extraction_result_saved",
        "context_committed",
        "extraction_batch_committed",
    ],
)
def test_subprocess_exit_before_after_commit_replays_without_new_model_or_revision(tmp_path, point):
    with session_for(tmp_path) as session:
        seed(session)
    code = """
import os, sys
from tests.plan_support import session_for
from tests.test_task_extraction import extraction, output, rule
with session_for(sys.argv[1]) as session:
    service, client = extraction(session, lambda data, call: output([rule(data, 'Keep API')]))
    session.fault = lambda p: os._exit(78) if p == sys.argv[2] else None
    service.process_user_message('crash', '保留 API')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), point],
        timeout=30,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONPATH": os.getcwd(), "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 78, result.stderr
    with session_for(tmp_path) as session:
        service, client = extraction(session, lambda data, call: output([rule(data, "Keep API")]))
        with pytest.raises(ContextBuildBlockedError):
            session.require_extraction_resolved()
        service.resume_pending()
        assert len(client.prompts) == (1 if point == "extraction_intake_saved" else 0)
        assert session.long_task_state.hard_constraints == ["Keep tests", "Keep API"]
        assert len(session.long_task_state.constraint_records) == 2
        budget = service.agent._run_budget_manager.snapshot()
        assert len(budget["model_calls"]) == 1
        session.verify_checkpoint(session.checkpoint())


def test_saved_segment_resume_uses_existing_result_and_does_not_refund_budget(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)
        service, client = extraction(session, lambda data, call: output([rule(data, "Keep API")]))

        def fault(point):
            if point == "extraction_segment_saved":
                raise CancelledError("interrupted")

        session.fault = fault
        with pytest.raises(CancelledError):
            service.process_user_message("resume", "保留 API")
        budget = service.agent._run_budget_manager
        budget.reserve("llm_calls", 2)
        session.fault = None
        restored = TaskExtraction(service.agent, session)
        restored.resume_pending()
        assert len(client.prompts) == 1 and budget.summary()["llm_calls"]["used"] == 3


def test_stale_snapshot_after_model_response_cannot_commit(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)
        service, client = extraction(session, lambda data, call: output([rule(data, "Keep API")]))

        def fault(point):
            if point == "extraction_result_saved":
                session.long_task.record_fact("new audit", kind="audit_note")
                session._persist_long_task_state()

        session.fault = fault
        with pytest.raises(ContextBuildBlockedError, match="extraction_snapshot_stale"):
            service.process_user_message("stale", "保留 API")
        assert session.long_task_state.hard_constraints == ["Keep tests"]


def test_cancelled_call_cannot_publish_and_reservation_is_durable(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)
        service, client = extraction(session)

        def cancelled(data, call):
            service.agent.cancel_token.cancel()
            raise CancelledError()

        client.respond = cancelled
        with pytest.raises(CancelledError):
            service.process_user_message("cancel", "保留 API")
        assert session.long_task_state.hard_constraints == ["Keep tests"]
        snapshot = session.store.latest("extraction_budget", "budget_id").popitem()[1]["snapshot"]
        assert next(iter(snapshot["model_calls"].values()))["usage_status"] == "unknown"


def test_worker_cannot_enroll_source_or_dispatch_extraction(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)
        service, client = extraction(session)
        errors = []

        def worker():
            try:
                service.process_user_message("worker", "I am user")
            except ValueError as exc:
                errors.append(str(exc))

        before = deepcopy(session.store.events())
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert errors == ["owner_thread_required"] and not client.prompts
        assert session.store.events() == before


def test_deferred_choice_cannot_disappear_when_plan_is_bound(tmp_path):
    with session_for(tmp_path) as session:
        seed(session, plan=False)

        def respond(data, call):
            if call > 1:
                return output()
            return output(
                deferred=[
                    {
                        "kind": "decision",
                        "action": "add",
                        "statement": "Preserve API",
                        "source_refs": [anchor(data)],
                    }
                ]
            )

        service, client = extraction(session, respond)
        service.process_user_message("choice", "Choose to preserve API")
        session.create(simple_plan(session))
        with pytest.raises(ContextBuildBlockedError, match="clarification_required"):
            service.resume_pending()
        assert len(client.prompts) == 2 and not session.long_task_state.key_decisions
        with pytest.raises(ContextBuildBlockedError):
            session.build_long_task_context("edit")


def test_empty_clarification_output_cannot_unblock_original_requirements(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)
        service, client = extraction(
            session,
            lambda data, call: (
                output(clarifications=[{"question": "Which API?", "source_refs": [anchor(data)]}])
                if call == 1
                else output()
            ),
        )
        with pytest.raises(ContextBuildBlockedError):
            service.process_user_message("unclear", "Use API v1 and v2")
        with pytest.raises(ContextBuildBlockedError):
            service.process_user_message("answer", "Use API v2", resolves=["unclear"])
        assert not session.long_task_state.key_decisions
        with pytest.raises(ContextBuildBlockedError):
            session.require_extraction_resolved()


def test_preplan_clarification_choice_can_wait_for_plan_without_old_conflict_deadlock(tmp_path):
    with session_for(tmp_path) as session:
        seed(session, plan=False)

        def respond(data, call):
            if call == 1:
                return output(
                    clarifications=[{"question": "Choose v1 or v2?", "source_refs": [anchor(data)]}]
                )
            choice = {
                "kind": "decision",
                "action": "add",
                "statement": "Use API v2",
                "source_refs": [anchor(data)],
            }
            if not data["state"]["nodes"]:
                return output(deferred=[choice])
            choice["node_id"] = "edit"
            return output([choice])

        service, client = extraction(session, respond)
        with pytest.raises(ContextBuildBlockedError):
            service.process_user_message("unclear-choice", "Choose v1 and v2")
        assert (
            service.process_user_message("answer-choice", "Choose v2", resolves=["unclear-choice"])[
                "phase"
            ]
            == "awaiting_node"
        )
        service.resume_pending()  # Old ambiguity no longer prevents Plan creation.
        with pytest.raises(ContextBuildBlockedError):
            session.require_extraction_resolved()
        session.create(simple_plan(session))
        service.resume_pending()
        session.require_extraction_resolved()
        assert len(client.prompts) == 3
        assert session.long_task_state.key_decisions[0]["statement"] == "Use API v2"


def test_duplicate_json_keys_cannot_hide_a_candidate(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)
        service, client = extraction(
            session,
            lambda data, call: output().replace('"updates": []', '"updates": [{}], "updates": []'),
        )
        with pytest.raises(ContextBuildBlockedError):
            service.process_user_message("duplicate", "Preserve API")
        assert len(client.prompts) == 2 and session.long_task_state.hard_constraints == [
            "Keep tests"
        ]


def test_pending_clarification_recovers_before_older_blocked_source(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)
        service, _ = extraction(
            session,
            lambda data, call: output(
                clarifications=[{"question": "Which API?", "source_refs": [anchor(data)]}]
            ),
        )
        with pytest.raises(ContextBuildBlockedError):
            service.process_user_message("unclear", "Use v1 and v2")

        def fault(point):
            if point == "extraction_intake_saved":
                raise CancelledError()

        session.fault = fault
        with pytest.raises(CancelledError):
            service.process_user_message("answer", "Preserve API v2", resolves=["unclear"])
    with session_for(tmp_path) as restored:
        service, client = extraction(
            restored, lambda data, call: output([rule(data, "Preserve API v2")])
        )
        service.resume_pending()
        restored.require_extraction_resolved()
        assert len(client.prompts) == 1
        assert restored.long_task_state.hard_constraints == ["Keep tests", "Preserve API v2"]


def test_chained_clarifications_keep_all_sources_and_settle_all_old_questions(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)

        def respond(data, call):
            if call < 3:
                return output(
                    clarifications=[
                        {"question": "Please state final rule", "source_refs": [anchor(data)]}
                    ]
                )
            assert {s["message_id"] for s in data["clarification_sources"]} == {"first", "second"}
            return output([rule(data, "Preserve API and docs")])

        service, client = extraction(session, respond)
        with pytest.raises(ContextBuildBlockedError):
            service.process_user_message("first", "Preserve docs and API v1 or v2")
        with pytest.raises(ContextBuildBlockedError):
            service.process_user_message("second", "Choose v1 or v2?", resolves=["first"])
        service.process_user_message("third", "Preserve API v2 and docs", resolves=["second"])
        session.require_extraction_resolved()
        service.resume_pending()
        assert len(client.prompts) == 3
        assert session.long_task_state.hard_constraints == ["Keep tests", "Preserve API and docs"]


def test_format_repair_is_bounded_across_all_segments(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)
        service, client = extraction(
            session, lambda data, call: output() if call == 2 else "invalid", limit=2600
        )
        with pytest.raises(ContextBuildBlockedError):
            service.process_user_message("long-invalid", "background details.\n" * 450)
        assert len(client.prompts) == 3  # First part repairs once; next part cannot repair.
        assert session.long_task_state.hard_constraints == ["Keep tests"]


def test_expired_deadline_blocks_extraction_before_dispatch(tmp_path):
    from agent_runtime.repair_runtime import ExecutionDeadline

    with session_for(tmp_path) as session:
        seed(session)
        service, client = extraction(session)
        service.agent._repair_deadline = ExecutionDeadline.from_remaining(0)
        with pytest.raises(ContextBuildBlockedError):
            service.process_user_message("deadline", "Preserve API")
        assert not client.prompts
        assert not service.agent._run_budget_manager.snapshot()["model_calls"]


def test_source_not_offered_cannot_be_imported_by_extractor(tmp_path):
    with session_for(tmp_path) as session:
        seed(session)
        session.register_user_message("unrelated", "Keep docs")
        ref = session.source_reference("unrelated")
        service, client = extraction(
            session, lambda data, call: output([{**rule(data, "Keep docs"), "source_refs": [ref]}])
        )
        with pytest.raises(ContextBuildBlockedError):
            service.process_user_message("current", "Preserve API")
        assert len(client.prompts) == 2 and session.long_task_state.hard_constraints == [
            "Keep tests"
        ]


def test_evidence_changed_after_extraction_cannot_publish_technical_choice(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        seed(session)
        through_analysis(session)
        service, client = extraction(
            session,
            lambda data, call: output(
                [
                    {
                        "kind": "decision",
                        "action": "add",
                        "statement": "Keep implementation",
                        "node_id": "edit",
                        "source_refs": [anchor(data)],
                        "evidence_refs": data["state"]["evidence_refs"],
                    }
                ]
            ),
        )

        def fault(point):
            if point == "extraction_result_saved":
                (tmp_path / "value.py").write_text("external = 9\n")

        session.fault = fault
        with pytest.raises(ContextBuildBlockedError, match="extraction_evidence_changed"):
            service.process_model_message(
                "stale-evidence",
                "I choose to keep implementation",
                node_id="edit",
                evidence_refs=session.plan.node("analysis").output_evidence_refs,
            )
        assert not session.long_task_state.key_decisions
