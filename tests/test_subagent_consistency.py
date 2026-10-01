"""Delegation views, real child reads and owner-visible admission diagnostics."""

import copy
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agent_runtime.model_turn import ModelTurnResult, ToolCall
from agent_runtime.plan_runtime.models import digest
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.read_permits import read_permits
from src.collaboration.exploration_projection import CONTEXT_MAX_BYTES
from tests.plan_support import session_for, simple_plan
from tests.test_exploration_runtime import REQUESTS, DiscoveryClient, finish, make_runtime

USAGE = {"input_tokens": 10, "output_tokens": 10}


class TurnClient:
    def __init__(self, turn):
        self.turn, self.requests = turn, []

    def complete_turn(self, request):
        self.requests.append(copy.deepcopy(request))
        if len(self.requests) == 1:
            return self.turn
        return ModelTurnResult(
            text=json.dumps({"summary": "Inspected", "findings": [], "unknowns": []}),
            usage=USAGE,
        )


def raw_blocks(calls):
    return [
        {"type": "tool_use", "id": c.call_id, "name": c.name, "input": copy.deepcopy(c.arguments)}
        for c in calls
    ]


@pytest.mark.parametrize(
    "damage",
    [
        "id",
        "name",
        "input",
        "order",
        "missing",
        "duplicate_id",
        "empty_id",
        "unknown",
        "structure",
        "unpaired",
    ],
)
def test_native_protocol_rejection_performs_zero_reads(tmp_path, damage):
    calls = [
        ToolCall("read_file", {"path": "value.py"}, "one"),
        ToolCall("read_file", {"path": "test_value.py"}, "two"),
    ]
    content = raw_blocks(calls)
    if damage == "id":
        content[1]["id"] = "other"
    elif damage == "name":
        content[1]["name"] = "grep"
    elif damage == "input":
        content[1]["input"] = {"path": "value.py"}
    elif damage == "order":
        content.reverse()
    elif damage == "missing":
        content.pop()
    elif damage in {"duplicate_id", "empty_id"}:
        calls[1] = ToolCall(
            "read_file", {"path": "test_value.py"}, "one" if damage == "duplicate_id" else ""
        )
        content = raw_blocks(calls)
    elif damage == "unknown":
        calls[1] = ToolCall("write_file", {"path": "value.py", "content": "changed"}, "two")
        content = raw_blocks(calls)
    elif damage == "structure":
        calls[1] = ToolCall("read_file", None, "two")
        content = raw_blocks(calls)
    else:
        calls = []
    client = TurnClient(ModelTurnResult(tool_calls=calls, content=content, usage=USAGE))
    runtime = make_runtime(tmp_path, client_factory=lambda t: client)
    try:
        before = (tmp_path / "value.py").read_text()
        result = finish(runtime, [runtime.delegate(REQUESTS[:1])[0]["handle"]])
        task = result["tasks"][0]
        assert task["status"] == "partial" and not result["findings"]
        assert task["error_code"] == "tool_batch_protocol_error"
        assert task["usage"]["tool_calls"] == 0 and task["observations"] == []
        assert len(client.requests) == 1
        assert not any(e["event_type"] == "subagent_tool_started" for e in runtime.store.events())
        assert (tmp_path / "value.py").read_text() == before
        assert task["diagnostics"] == [{"kind": "execution", "reason": "tool_batch_protocol_error"}]
    finally:
        runtime.close()


def test_schema_denial_is_paired_and_legal_sibling_is_read_serially(tmp_path):
    calls = [ToolCall("read_file", {}, "bad"), ToolCall("read_file", {"path": "value.py"}, "good")]
    content = [{"type": "text", "text": "Inspect sources"}, *raw_blocks(calls)]
    client = TurnClient(ModelTurnResult(tool_calls=calls, content=content, usage=USAGE))
    runtime = make_runtime(tmp_path, client_factory=lambda t: client)
    try:
        result = finish(runtime, [runtime.delegate(REQUESTS[:1])[0]["handle"]])
        task = result["tasks"][0]
        assert (
            task["status"] == "partial" and task["error_code"] == "retrieval_incomplete_or_rejected"
        )
        assert task["usage"]["tool_calls"] == 2  # Child's cap includes rejected attempts.
        assert client.requests[1].messages[-2]["content"] == content
        replies = client.requests[1].messages[-1]["content"]
        assert [r["tool_use_id"] for r in replies] == ["bad", "good"]
        assert json.loads(replies[0]["content"])["error_code"] == "invalid_arguments"
        assert json.loads(replies[1]["content"])["hits"][0]["path"] == "value.py"
        observations = runtime._observations(runtime.tasks()[0].payload["exploration"])
        try:
            assert [observations.get(o["observation_id"]).status for o in task["observations"]] == [
                "error",
                "ok",
            ]
        finally:
            observations.close()
        assert not result["findings"]
    finally:
        runtime.close()


@pytest.mark.parametrize("invalid", [False, True], ids=["valid", "invalid_argument"])
def test_xml_single_call_preflight_keeps_pairing(tmp_path, invalid):
    client = FakeModelClient(
        [
            "<tool>"
            + json.dumps({"name": "read_file", "args": {} if invalid else {"path": "value.py"}})
            + "</tool>",
            '<final>{"summary":"Done","findings":[],"unknowns":[]}</final>',
        ]
    )
    runtime = make_runtime(tmp_path, client_factory=lambda t: client)
    try:
        result = finish(runtime, [runtime.delegate(REQUESTS[:1])[0]["handle"]])
        task = result["tasks"][0]
        assert task["status"] == ("partial" if invalid else "completed")
        assert task["usage"]["tool_calls"] == 1
        assert task["observations"][0]["complete"] is not invalid
    finally:
        runtime.close()


def attach_plan(runtime, session):
    session.configure_long_task(
        "Fix answer while preserving its public API", hard_constraints=["Keep the public API"]
    )
    session.create(simple_plan(session))
    session.local.attempt = session.prepare("read-0")
    runtime.plan_session = session


def test_delegated_view_is_detached_bounded_and_restored_without_model_call(tmp_path):
    clients = []

    def factory(task):
        client = DiscoveryClient(task)
        clients.append(client)
        return client

    runtime = make_runtime(tmp_path, parent="task", client_factory=factory)
    try:
        with session_for(tmp_path) as session:
            attach_plan(runtime, session)
            view = session.plan_view("read-0")
            handles = [runtime.delegate(REQUESTS[:1])[0]["handle"]]
            assert finish(runtime, handles)["findings"]
            context = json.loads(clients[0].requests[0].messages[0]["content"])["context"]
            assert context["current_node"] == view["nodes"][0]
            assert context["plan_view"] == {
                k: v for k, v in view.items() if k not in {"nodes", "active_node_ids"}
            }
            assert context["hard_constraints"] == ["Keep the public API"]
            assert context["goal"] == session.long_task_state.original_request
            assert len(json.dumps(context, ensure_ascii=False).encode()) <= CONTEXT_MAX_BYTES
            assert "PRIVATE_PARENT_CONVERSATION" not in str(clients[0].requests)
            context["current_node"]["objective"] = "modified display"
            assert session.plan_view("read-0") == view
            stored = runtime.tasks()[0].payload["exploration"]["delegation_context"]
            assert stored["current_node"] == view["nodes"][0]
            session.long_task.record_fact("Unrelated audit", kind="audit_note")
            session._persist_long_task_state()
            runtime.restore(runtime.checkpoint())
            assert len(clients) == 1 and len(clients[0].requests) == 2
            assert runtime.collect(handles)["tasks"][0]["validation"]["status"] == "valid"
    finally:
        runtime.close()


def test_collect_reports_cleanup_unconfirmed_and_retains_write_gate(tmp_path):
    entered, release = threading.Event(), threading.Event()
    runtime = make_runtime(
        tmp_path,
        client_factory=lambda t: DiscoveryClient(t, entered=entered, release=release),
    )
    try:
        handle = runtime.delegate(REQUESTS[:1])[0]["handle"]
        assert entered.wait(5)
        runtime._request_cancel()
        result = runtime.collect([handle])
        assert result["tasks"][0]["status"] == "worker_lost"
        assert {"kind": "cleanup", "reason": "exploration_cleanup_unconfirmed"} in result["tasks"][
            0
        ]["diagnostics"]
        with pytest.raises(ValueError, match="exploration_cleanup_unconfirmed"):
            runtime.before_write()
        assert not result["findings"]
        release.set()
        for worker in runtime.workers.values():
            worker.join(5)
        task = runtime.collect([handle])["tasks"][0]
        assert task["status"] == "cancelled" and task["cleanup_confirmed"]
        assert not any(d["kind"] == "cleanup" for d in task["diagnostics"])
    finally:
        release.set()
        runtime.close()


def test_oversized_goal_or_constraint_rejects_before_task_creation(tmp_path):
    runtime = make_runtime(tmp_path, parent="task")
    try:
        with session_for(tmp_path) as session:
            attach_plan(runtime, session)
            session.configure_long_task("Fix answer", hard_constraints=["保留" * CONTEXT_MAX_BYTES])
            before = runtime.budget.snapshot()
            with pytest.raises(ValueError, match="exploration_context_budget_exceeded"):
                runtime.delegate(REQUESTS)
            assert not runtime.tasks() and runtime.budget.snapshot() == before
            assert not runtime.workers
    finally:
        runtime.close()


def test_queued_context_tamper_blocks_provider_before_first_turn(tmp_path):
    clients = []
    runtime = make_runtime(tmp_path, parent="task", client_factory=lambda t: clients.append(t))
    permits = read_permits(runtime.root, runtime.run_id)
    assert permits.acquire() and permits.acquire()
    try:
        with session_for(tmp_path) as session:
            attach_plan(runtime, session)
            handle = runtime.delegate(REQUESTS[:1])[0]["handle"]

            def tamper(task, conn):
                task.payload["exploration"]["delegation_context"]["hard_constraints"] = []

            runtime.store.mutate(handle, tamper, "test_context_tamper")
            permits.release()
            permits.release()
            result = finish(runtime, [handle])
            assert result["tasks"][0]["error_code"] == "exploration_plan_context_invalid"
            assert not clients and not result["findings"]
    finally:
        runtime.close()


@pytest.mark.parametrize("change", ["goal", "constraints"])
def test_current_task_context_change_marks_old_result_stale(tmp_path, change):
    runtime = make_runtime(tmp_path, parent="task")
    try:
        with session_for(tmp_path) as session:
            attach_plan(runtime, session)
            handle = runtime.delegate(REQUESTS[:1])[0]["handle"]
            assert finish(runtime, [handle])["findings"]
            session.configure_long_task(
                "Different goal" if change == "goal" else session.long_task_state.original_request,
                hard_constraints=["New constraint"]
                if change == "constraints"
                else ["Keep the public API"],
            )
            result = runtime.collect([handle])
            assert result["tasks"][0]["validation"] == {
                "status": "stale",
                "reason": "task_context_changed",
            }
            assert not result["findings"]
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "damage,reason",
    [
        ("workspace", "workspace_changed"),
        ("checksum", "result_checksum_invalid"),
        ("blob", "observation_checksum_invalid"),
        ("observation", "observation_missing"),
    ],
)
def test_collection_reports_concrete_stale_reason_without_reexecution(tmp_path, damage, reason):
    clients = []

    def factory(task):
        client = DiscoveryClient(task)
        clients.append(client)
        return client

    runtime = make_runtime(tmp_path, client_factory=factory)
    try:
        handle = runtime.delegate(REQUESTS[:1])[0]["handle"]
        assert finish(runtime, [handle])["findings"]
        if damage == "workspace":
            (tmp_path / "value.py").write_text("changed\n")
        elif damage in {"checksum", "observation"}:

            def mutate(task, conn):
                result = task.payload["exploration"]["result"]
                if damage == "checksum":
                    result["summary"] = "tampered"
                else:
                    result["observations"][0]["observation_id"] = "OBS-missing"
                    result["checksum"] = digest(
                        {k: v for k, v in result.items() if k != "checksum"}
                    )

            runtime.store.mutate(handle, mutate, "test_result_tamper")
        else:
            data = runtime.tasks()[0].payload["exploration"]
            observations = runtime._observations(data)
            try:
                record = observations.get(data["result"]["observations"][0]["observation_id"])
                Path(record.raw_ref).write_text("corrupted")
            finally:
                observations.close()
        result = runtime.collect([handle])
        task = result["tasks"][0]
        assert task["validation"] == {"status": "stale", "reason": reason}
        assert task["diagnostics"] == [{"kind": "validation", "reason": reason}]
        assert not result["findings"]
        assert len(clients) == 1 and len(clients[0].requests) == 2
    finally:
        runtime.close()


def test_process_recovery_reuses_delegation_snapshot_with_new_attempt(tmp_path):
    code = """
import json, os, sys, threading
from pathlib import Path
from tests.test_exploration_runtime import make_runtime, REQUESTS, DiscoveryClient
from tests.test_subagent_consistency import attach_plan
from tests.plan_support import session_for
entered, release = threading.Event(), threading.Event()
root = Path(sys.argv[1])
runtime = make_runtime(root, parent='task', client_factory=lambda t: DiscoveryClient(t, entered=entered, release=release))
session = session_for(root)
attach_plan(runtime, session)
runtime.delegate(REQUESTS[:1])
assert entered.wait(5)
print(json.dumps(runtime.checkpoint()), flush=True)
os._exit(74)
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PYTHONPATH": os.getcwd()},
    )
    assert child.returncode == 74, child.stderr
    seal = json.loads(child.stdout.strip())
    clients = []

    def factory(task):
        client = DiscoveryClient(task)
        clients.append(client)
        return client

    runtime = make_runtime(tmp_path, parent="task", client_factory=factory)
    try:
        with session_for(tmp_path) as session:
            runtime.plan_session = session
            handle = seal["tasks"][0]["task_id"]
            original = runtime.store.get_task(handle)
            context = original.payload["exploration"]["delegation_context"]
            runtime.restore(seal)
            result = finish(runtime, [handle])
            data = runtime.tasks()[0].payload["exploration"]
            assert result["findings"] and result["tasks"][0]["validation"]["status"] == "valid"
            assert data["delegation_context"] == context
            assert data["attempt_id"] != original.payload["exploration"]["attempt_id"]
            projection = json.loads(clients[0].requests[0].messages[0]["content"])
            assert projection["context"] == context
            assert len(data["receipts"]) == 2
            assert data["receipts"][0]["charged_tokens"] == runtime.limits.tokens
            (tmp_path / "delegation-recovery.json").write_text(
                json.dumps(
                    {
                        "context": context,
                        "old_attempt": original.payload["exploration"]["attempt_id"],
                        "new_attempt": data["attempt_id"],
                        "receipts": data["receipts"],
                        "validation": result["tasks"][0]["validation"],
                    },
                    indent=2,
                )
            )
    finally:
        runtime.close()


def test_result_error_keeps_contract_reason_without_exposing_exception_text(tmp_path):
    turn = ModelTurnResult(
        text=json.dumps(
            {
                "summary": "Claim",
                "findings": [
                    {
                        "claim_key": "foreign",
                        "statement": "Located",
                        "path": "value.py",
                        "range": None,
                        "observation_id": "OBS-foreign",
                    }
                ],
            }
        ),
        usage=USAGE,
    )
    runtime = make_runtime(tmp_path, client_factory=lambda t: TurnClient(turn))
    try:
        task = finish(runtime, [runtime.delegate(REQUESTS[:1])[0]["handle"]])["tasks"][0]
        assert task["status"] == "partial"
        assert task["error_code"] == "claim_observation_not_owned"
        assert task["diagnostics"] == [
            {"kind": "execution", "reason": "claim_observation_not_owned"}
        ]
    finally:
        runtime.close()


def test_foreign_plan_scope_rejects_before_reservation(tmp_path):
    runtime = make_runtime(tmp_path, parent="foreign")
    try:
        with session_for(tmp_path) as session:
            attach_plan(runtime, session)
            before = runtime.budget.snapshot()
            with pytest.raises(ValueError, match="exploration_plan_context_scope_mismatch"):
                runtime.delegate(REQUESTS)
            assert not runtime.tasks() and runtime.budget.snapshot() == before
    finally:
        runtime.close()
