"""Actual bounded child Agent loops and filesystem readers, with controlled models."""

import copy
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from agent_runtime.budget_manager import BudgetManager
from agent_runtime.cancellation import CancellationToken, CancelledError
from agent_runtime.model_turn import FinishKind, ModelTurnResult, ProviderFinish, ToolCall
from agent_runtime.plan_runtime.models import digest
from agent_runtime.plan_runtime.workspace import snapshot
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.read_permits import read_permits
from agent_runtime.workspace import WorkspaceContext
from src.agents.explorer import ExplorerToken, create_explorer_agent
from src.agents.factory import create_repair_agent
from src.collaboration.exploration_contracts import ExplorationLimits
from src.collaboration.exploration_results import merge_findings
from src.collaboration.exploration_runtime import ExplorationRuntime
from src.collaboration.store import LeaseConflictError

REQUESTS = [
    {"kind": "implementation_location", "question": "Locate answer's implementation."},
    {"kind": "related_tests", "question": "Find existing tests for answer."},
]


class DiscoveryClient:
    def __init__(self, task, *, barrier=None, entered=None, release=None, mutate=None, bad=False):
        self.kind = task.kind
        self.barrier, self.entered, self.release, self.mutate, self.bad = (
            barrier,
            entered,
            release,
            mutate,
            bad,
        )
        self.requests = []

    def complete_turn(self, request):
        self.requests.append(copy.deepcopy(request))
        usage = {"input_tokens": 100, "output_tokens": 50}
        if len(self.requests) == 1:
            if self.entered:
                self.entered.set()
            if self.barrier:
                self.barrier.wait(5)  # Both real loops must be inside a model turn together.
            if self.release:
                assert self.release.wait(5)
            path = "value.py" if self.kind == "implementation_location" else "test_value.py"
            return ModelTurnResult(
                tool_calls=[ToolCall("read_file", {"path": path}, "read")],
                finish=ProviderFinish(FinishKind.TOOL_CALLS),
                usage=usage,
            )
        if self.bad:
            raise RuntimeError("controlled provider failure")
        visible = json.loads(request.messages[-1]["content"][0]["content"])
        hit = visible["hits"][0]
        if self.mutate:
            self.mutate()
        raw = {
            "summary": "Located source",
            "findings": [
                {
                    "claim_key": self.kind,
                    "statement": "Observed answer source"
                    if hit["path"] == "value.py"
                    else "Candidate test for answer; coverage unverified",
                    "path": hit["path"],
                    "range": hit["range"],
                    "observation_id": visible["observation_id"],
                }
            ],
            "unknowns": [],
        }
        return ModelTurnResult(
            text=json.dumps(raw), finish=ProviderFinish(FinishKind.TEXT_COMPLETE), usage=usage
        )


def make_runtime(root, *, limits=None, client_factory=None, run_id="run", parent="parent"):
    (root / "value.py").write_text("def answer():\n    return 1\n", encoding="utf-8")
    (root / "test_value.py").write_text(
        "from value import answer\ndef test_answer():\n    assert answer() == 2\n", encoding="utf-8"
    )
    workspace = WorkspaceContext(cwd=str(root), repo_root=str(root))
    agent = create_repair_agent("patcher", FakeModelClient([]), workspace, cwd=str(root))
    agent.cancel_token = CancellationToken()
    agent.session["history"] = [{"role": "user", "content": "PRIVATE_PARENT_CONVERSATION"}]
    runtime = ExplorationRuntime(
        agent,
        run_id=run_id,
        parent_task_id=parent,
        limits=limits,
        client_factory=client_factory or DiscoveryClient,
    )
    return runtime


def finish(runtime, handles):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = runtime.collect(handles, wait_ms=1000)
        if all(t["status"] not in {"queued", "running"} for t in value["tasks"]):
            return value
    raise AssertionError(runtime.tasks())


def test_restore_queued_task_keeps_original_worker_handle(tmp_path):
    runtime = make_runtime(tmp_path)
    gate = read_permits(runtime.root, runtime.run_id)
    assert gate.acquire() and gate.acquire()
    try:
        try:
            handles = [t["handle"] for t in runtime.delegate(REQUESTS)]
            original = dict(runtime.workers)
            runtime.restore(runtime.checkpoint())
            assert runtime.workers == original
        finally:
            gate.release()
            gate.release()
        assert all(t["status"] == "completed" for t in finish(runtime, handles)["tasks"])
    finally:
        runtime.close()


def test_fixed_task_serial_parallel_measurement(tmp_path):
    measurements = {}
    for mode in ("serial", "parallel"):
        root = tmp_path / mode
        root.mkdir()
        lock = threading.Lock()
        counts = {"active": 0, "peak": 0}
        clients = []

        class MeasuredClient(DiscoveryClient):
            def complete_turn(self, request):
                with lock:
                    counts["active"] += 1
                    counts["peak"] = max(counts["peak"], counts["active"])
                try:
                    time.sleep(0.05)  # Controlled provider latency, not live API performance.
                    return super().complete_turn(request)
                finally:
                    with lock:
                        counts["active"] -= 1

        def factory(task):
            client = MeasuredClient(task)
            clients.append(client)
            return client

        runtime = make_runtime(root, client_factory=factory)
        started = time.monotonic()
        findings = []
        try:
            batches = [REQUESTS] if mode == "parallel" else [[r] for r in REQUESTS]
            for batch in batches:
                handles = [t["handle"] for t in runtime.delegate(batch)]
                findings.extend(finish(runtime, handles)["findings"])
            tasks = runtime.tasks()
            usage = [t.payload["exploration"]["result"]["usage"] for t in tasks]
            measurements[mode] = {
                "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                "model_calls": sum(len(c.requests) for c in clients),
                "tool_calls": sum(u["tool_calls"] for u in usage),
                "tokens": sum(u["tokens"] for u in usage),
                "peak_model_concurrency": counts["peak"],
                "paths": sorted({f["path"] for f in findings}),
                "valid_findings": len(findings),
                "duplicate_findings": len(findings) - len({f["path"] for f in findings}),
            }
        finally:
            runtime.close()
    assert measurements["serial"]["peak_model_concurrency"] == 1
    assert measurements["parallel"]["peak_model_concurrency"] == 2
    assert measurements["serial"]["paths"] == measurements["parallel"]["paths"]
    report = {
        "mode": "controlled ModelClient responses and 50 ms latency; actual Agent/tool loops",
        "measurements": measurements,
        "extra_tokens": measurements["parallel"]["tokens"] - measurements["serial"]["tokens"],
        "live_provider_performance_measured": False,
    }
    (tmp_path / "serial-parallel-measurement.json").write_text(json.dumps(report, indent=2))


def test_provider_cancellation_without_usage_charges_reserved_cap(tmp_path):
    class CancelledClient:
        def complete_turn(self, request):
            raise CancelledError("provider_cancelled")

    runtime = make_runtime(tmp_path, client_factory=lambda task: CancelledClient())
    try:
        handles = [t["handle"] for t in runtime.delegate(REQUESTS[:1])]
        result = finish(runtime, handles)
        assert result["tasks"][0]["status"] == "cancelled"
        data = runtime.tasks()[0].payload["exploration"]
        assert data["result"]["usage"]["tokens"] is None
        assert data["charged_tokens"] == runtime.limits.tokens
    finally:
        runtime.close()


def test_real_parallel_loops_handles_evidence_and_isolated_context(tmp_path):
    barrier, clients = threading.Barrier(2), []

    def factory(task):
        client = DiscoveryClient(task, barrier=barrier)
        clients.append(client)
        return client

    runtime = make_runtime(tmp_path, client_factory=factory)
    before = snapshot(str(tmp_path))
    try:
        submitted = runtime.delegate(REQUESTS)
        handles = [t["handle"] for t in submitted]
        result = finish(runtime, handles)
        assert {t["status"] for t in result["tasks"]} == {"completed"}, result
        assert len(clients) == 2 and barrier.n_waiting == 0
        assert len(result["findings"]) == 2
        assert all(len(c.requests) == 2 for c in clients)
        assert "PRIVATE_PARENT_CONVERSATION" not in str(clients[0].requests)
        assert all(
            f["file_hash"] == before[f["path"]]
            and f["sources"][0]["checksum"]
            and f["review"] == "candidate"
            for f in result["findings"]
        )
        assert snapshot(str(tmp_path)) == before
        event_count = len(runtime.store.events(run_id="run"))
        charges = [t.payload["exploration"]["charged_tokens"] for t in runtime.tasks()]
        assert runtime.collect(handles) == result
        assert len(runtime.store.events(run_id="run")) == event_count
        assert [t.payload["exploration"]["charged_tokens"] for t in runtime.tasks()] == charges
        projection = runtime.progress()
        assert not projection["progress_replay_incomplete"]
        calls = next(iter(projection["turns"].values()))["calls"]
        assert len(calls) == 2 and all(c["status"] == "completed" for c in calls)
        events = runtime.store.progress_events("run")
        assert "Observed answer source" not in json.dumps(events)
        assert "PRIVATE_PARENT_CONVERSATION" not in json.dumps(events)
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "name,args",
    [
        ("write_file", {"path": "value.py", "content": "evil"}),
        ("apply_patch", {"patch": "evil"}),
        ("patch_file", {"path": "value.py"}),
        ("run_shell", {"command": "python -c 'print(1)'"}),
        ("quick_test", {"nodeid": "test_value.py"}),
        ("delegate_exploration", {"tasks": REQUESTS}),
    ],
)
def test_explorer_both_gate_layers_deny_writes_and_execution(tmp_path, name, args):
    runtime = make_runtime(tmp_path)
    token = ExplorerToken(None, time.time() + 5)
    child = create_explorer_agent(
        FakeModelClient([]), root=str(tmp_path), scopes=["."], token=token, limits=runtime.limits
    )
    before = snapshot(str(tmp_path))
    try:
        assert set(child.tools) == {"read_file", "list_files", "grep"}
        assert child.execute_tool(name, args).error_code == "permission_denied"
        assert not child._get_tool_executor().execute_gated(name, args).ok
        assert snapshot(str(tmp_path)) == before
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "requests",
    [
        [],
        REQUESTS * 2,
        [REQUESTS[0], REQUESTS[0]],
        [{**REQUESTS[0], "scope_paths": ["../outside"]}],
        [{**REQUESTS[0], "scope_paths": [".git"]}],
        [{**REQUESTS[0], "scope_paths": [".env"]}],
        [{**REQUESTS[0], "input_observation_ids": ["OBS-not-owned"]}],
        [{**REQUESTS[0], "max_tool_calls": 100}],
    ],
)
def test_invalid_whole_batch_leaves_no_tasks(tmp_path, requests):
    runtime = make_runtime(tmp_path)
    try:
        with pytest.raises(ValueError):
            runtime.delegate(requests)
        assert runtime.tasks() == []
    finally:
        runtime.close()


def test_collect_rejects_foreign_parent_and_wait_bounds(tmp_path):
    runtime = make_runtime(tmp_path)
    other = ExplorationRuntime(runtime.agent, run_id="run", parent_task_id="other-parent")
    try:
        handles = [v["handle"] for v in runtime.delegate(REQUESTS[:1])]
        with pytest.raises(ValueError, match="scope_mismatch"):
            other.collect(handles)
        with pytest.raises(ValueError, match="wait"):
            runtime.collect(handles, 1001)
        with pytest.raises(ValueError):
            runtime.collect(handles + ["foreign"])
        finish(runtime, handles)
    finally:
        other.close()
        runtime.close()


def test_scope_and_sensitive_paths_remain_enforced_on_reads(tmp_path):
    runtime = make_runtime(tmp_path)
    (tmp_path / ".env").write_text("SECRET=hidden")
    token = ExplorerToken(None, time.time() + 5)
    child = create_explorer_agent(
        FakeModelClient([]),
        root=str(tmp_path),
        scopes=["value.py"],
        token=token,
        limits=runtime.limits,
    )
    try:
        assert child.execute_tool("read_file", {"path": "value.py"}).ok
        for path in ["test_value.py", ".env", "../outside", ".agent/collaboration.db"]:
            assert not child.execute_tool("read_file", {"path": path}).ok
    finally:
        runtime.close()


def test_concurrency_budget_backpressure_and_independent_failure(tmp_path):
    entered, release = threading.Event(), threading.Event()
    clients = []

    def factory(task):
        client = DiscoveryClient(
            task, entered=entered, release=release, bad=task.kind == "implementation_location"
        )
        clients.append(client)
        return client

    runtime = make_runtime(tmp_path, client_factory=factory)
    try:
        handles = [t["handle"] for t in runtime.delegate(REQUESTS)]
        assert entered.wait(2)
        with pytest.raises(ValueError, match="concurrency"):
            runtime.delegate(REQUESTS[:1])
        assert len(runtime.tasks()) == 2
        assert all(t["status"] in {"queued", "running"} for t in runtime.collect(handles)["tasks"])
        release.set()
        result = finish(runtime, handles)
        assert {t["status"] for t in result["tasks"]} == {"failed", "completed"}
        failed = next(t for t in runtime.tasks() if t.kind == "implementation_location")
        assert (
            failed.payload["exploration"]["receipts"][0]["charged_tokens"] == runtime.limits.tokens
        )
        assert len(result["findings"]) == 1
    finally:
        release.set()
        runtime.close()


def test_batch_budget_is_atomic(tmp_path):
    runtime = make_runtime(tmp_path, limits=ExplorationLimits(run_tokens=24000))
    try:
        with pytest.raises(ValueError, match="budget"):
            runtime.delegate(REQUESTS)
        assert not runtime.tasks()
    finally:
        runtime.close()


def test_file_version_change_marks_evidence_stale(tmp_path):
    runtime = make_runtime(
        tmp_path,
        client_factory=lambda t: DiscoveryClient(
            t, mutate=lambda: (tmp_path / "value.py").write_text("def answer():\n    return 3\n")
        ),
    )
    try:
        handles = [t["handle"] for t in runtime.delegate(REQUESTS[:1])]
        result = finish(runtime, handles)
        assert result["tasks"][0]["status"] == "stale"
        assert result["findings"] == []
        assert any(e["event_type"] == "subagent_evidence_stale" for e in runtime.store.events())
    finally:
        runtime.close()


def test_observation_blob_tamper_rejects_completed_evidence(tmp_path):
    runtime = make_runtime(tmp_path)
    try:
        handles = [t["handle"] for t in runtime.delegate(REQUESTS[:1])]
        assert finish(runtime, handles)["findings"]
        task = runtime.tasks()[0]
        store = runtime._observations(task.payload["exploration"])
        source = task.payload["exploration"]["result"]["observations"][0]
        record = store.get(source["observation_id"])
        from pathlib import Path

        Path(record.raw_ref).write_text("corrupted")
        store.close()
        assert runtime.collect(handles)["tasks"][0]["status"] == "stale"
        assert not runtime.collect(handles)["findings"]
    finally:
        runtime.close()


def test_exact_duplicate_merge_conflict_and_different_versions():
    finding = {
        "claim_key": "symbol",
        "statement": "located",
        "path": "value.py",
        "range": None,
        "file_hash": "a",
        "scope": ["."],
        "category": "implementation_location",
        "resolution": "parsed",
        "review": "candidate",
        "sources": [{"observation_id": "one"}],
    }
    duplicate = {**finding, "sources": [{"observation_id": "two"}]}
    opposite = {**finding, "statement": "not located"}
    new_version = {**finding, "file_hash": "b"}
    result = merge_findings(
        [{"status": "completed", "findings": [finding, duplicate, opposite, new_version]}]
    )
    assert len(result) == 3
    assert len(result[0]["sources"]) == 2
    assert [f["review"] for f in result] == ["needs_review", "needs_review", "candidate"]
    assert not merge_findings([{"status": "partial", "findings": [finding]}])


def test_cancellation_lost_worker_late_result_and_retained_permit(tmp_path):
    entered, release = threading.Event(), threading.Event()
    runtime = make_runtime(
        tmp_path,
        limits=ExplorationLimits(cleanup_s=0.02),
        client_factory=lambda t: DiscoveryClient(t, entered=entered, release=release),
    )
    try:
        handles = [t["handle"] for t in runtime.delegate(REQUESTS[:1])]
        assert entered.wait(2)
        claimed = runtime.store.get_task(handles[0])
        runtime.parent_token.cancel()
        with pytest.raises(ValueError, match="cleanup_unconfirmed"):
            runtime.drain()
        data = runtime.store.get_task(handles[0]).payload["exploration"]
        assert data["status"] == "worker_lost" and not data["cleanup_confirmed"]
        with pytest.raises(LeaseConflictError):
            runtime.store.finish(claimed, {"status": "completed"})
        event_count = len(runtime.store.events(run_id="run"))
        runtime._emit(claimed, "subagent_model_completed", model_turn=1)
        assert len(runtime.store.events(run_id="run")) == event_count
        permits = read_permits(str(tmp_path), "run")
        assert permits.acquire()
        assert not permits.acquire()  # The lost worker still holds the second slot.
        permits.release()
        release.set()
        runtime.workers[handles[0]].join(2)
        data = runtime.store.get_task(handles[0]).payload["exploration"]
        assert data["cleanup_confirmed"] and data["status"] == "cancelled"
        assert not data.get("result_ref")
        assert len(data["receipts"]) == 1
    finally:
        release.set()
        runtime.close()


def test_checkpoint_completed_reuse_and_running_recovery_requires_stop(tmp_path):
    runtime = make_runtime(tmp_path)
    try:
        handles = [t["handle"] for t in runtime.delegate(REQUESTS[:1])]
        first = finish(runtime, handles)
        seal = runtime.checkpoint()
        calls = first["tasks"][0]["usage"]["model_turns"]
        runtime.restore(seal)
        assert runtime.collect(handles) == first
        assert runtime.collect(handles)["tasks"][0]["usage"]["model_turns"] == calls
        bad = {**seal, "run_id": "foreign"}
        bad["checksum"] = digest({k: v for k, v in bad.items() if k != "checksum"})
        with pytest.raises(ValueError, match="checkpoint"):
            runtime.restore(bad)
    finally:
        runtime.close()


def test_shared_model_budget_rejects_batch_without_partial_reservation(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.budget = BudgetManager({"llm_calls": 5, "tool_calls": 8, "prompt_tokens": 48000})
    runtime.agent._run_budget_manager = runtime.budget
    try:
        with pytest.raises(ValueError, match="global_budget"):
            runtime.delegate(REQUESTS)
        assert not runtime.tasks()
        assert all(v == 0 for v in runtime.budget.snapshot()["used"].values())
    finally:
        runtime.close()


def test_scope_budget_recovery_is_idempotent_and_preserves_owner_usage():
    budget = BudgetManager({"llm_calls": 10, "prompt_tokens": 10000})
    assert budget.reserve("llm_calls", 1).allowed
    assert budget.reserve_scope("child", {"llm_calls": 3, "prompt_tokens": 3000})
    assert budget.reserve_scope("child", {"llm_calls": 3, "prompt_tokens": 3000})
    budget.reconcile_scope("child", {"llm_calls": 2, "prompt_tokens": 1000})
    saved = budget.snapshot()
    restored = BudgetManager()
    restored.restore(saved)
    restored.reconcile_scope("child", {"llm_calls": 2, "prompt_tokens": 1000})
    assert restored.snapshot() == saved
    assert restored.snapshot()["used"]["llm_calls"] == 3


def test_live_old_worker_blocks_restore_and_write(tmp_path):
    entered, release = threading.Event(), threading.Event()
    runtime = make_runtime(
        tmp_path,
        limits=ExplorationLimits(cleanup_s=0.01),
        client_factory=lambda t: DiscoveryClient(t, entered=entered, release=release),
    )
    try:
        handles = [v["handle"] for v in runtime.delegate(REQUESTS[:1])]
        assert entered.wait(2)
        with pytest.raises(LeaseConflictError, match="unconfirmed"):
            runtime.restore(runtime.checkpoint())
        with pytest.raises(ValueError, match="cleanup_unconfirmed"):
            runtime.before_write()
        release.set()
        runtime.workers[handles[0]].join(2)
        assert runtime.tasks()[0].payload["exploration"]["cleanup_confirmed"]
    finally:
        release.set()
        runtime.close()


def test_process_crash_recovery_uses_new_attempt_and_conservative_durable_budget(tmp_path):
    code = """
import os, sys, threading, json
from pathlib import Path
from tests.test_exploration_runtime import make_runtime, REQUESTS, DiscoveryClient
entered, release = threading.Event(), threading.Event()
runtime = make_runtime(Path(sys.argv[1]), client_factory=lambda t: DiscoveryClient(t, entered=entered, release=release))
handles = [t['handle'] for t in runtime.delegate(REQUESTS[:1])]
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
    runtime = make_runtime(tmp_path)
    try:
        handle = seal["tasks"][0]["task_id"]
        original = runtime.store.get_task(handle)
        old_attempt = original.payload["exploration"]["attempt_id"]
        runtime.restore(seal)
        result = finish(runtime, [handle])
        task = runtime.store.get_task(handle)
        data = task.payload["exploration"]
        assert result["findings"] and data["attempt_id"] != old_attempt
        assert len(data["receipts"]) == 2
        assert data["receipts"][0]["charged_tokens"] == runtime.limits.tokens
        assert data["charged_tokens"] == runtime.limits.tokens + 300
        with pytest.raises(LeaseConflictError):
            runtime.store.finish(original, {"status": "completed"})
        snapshot_ = runtime.budget.snapshot()
        runtime.restore(runtime.checkpoint())
        assert runtime.budget.snapshot() == snapshot_
    finally:
        runtime.close()


def test_deadline_worker_receipt_and_no_new_dispatch_after_cancel(tmp_path):
    entered, release = threading.Event(), threading.Event()
    runtime = make_runtime(
        tmp_path,
        limits=ExplorationLimits(deadline_s=0.15, cleanup_s=0.01),
        client_factory=lambda t: DiscoveryClient(t, entered=entered, release=release),
    )
    try:
        handle = runtime.delegate(REQUESTS[:1])[0]["handle"]
        assert entered.wait(2)
        deadline = time.monotonic() + 2
        while runtime.store.get_task(handle).payload["exploration"]["status"] != "worker_lost":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        release.set()
        runtime.workers[handle].join(2)
        assert runtime.store.get_task(handle).payload["exploration"]["status"] == "timed_out"
        runtime.parent_token.cancel()
        from agent_runtime.cancellation import CancelledError

        with pytest.raises(CancelledError):
            runtime.delegate(REQUESTS)
        assert len(runtime.tasks()) == 1
    finally:
        release.set()
        runtime.close()


def test_partial_visible_output_never_becomes_successful_findings(tmp_path):
    runtime = make_runtime(tmp_path)
    (tmp_path / "value.py").write_text("def answer():\n    return 1\n" + "# observed line\n" * 190)
    try:
        handle = runtime.delegate(REQUESTS[:1])[0]["handle"]
        result = finish(runtime, [handle])
        assert result["tasks"][0]["status"] == "partial"
        assert result["findings"] == []
    finally:
        runtime.close()


def test_empty_result_records_limits_and_never_asserts_repository_absence(tmp_path):
    class EmptyClient:
        def complete_turn(self, request):
            return ModelTurnResult(
                text=json.dumps(
                    {
                        "summary": "No observed locations",
                        "findings": [],
                        "unknowns": ["Search has not covered the repository."],
                    }
                ),
                usage={"input_tokens": 10, "output_tokens": 10},
            )

    runtime = make_runtime(tmp_path, client_factory=lambda t: EmptyClient())
    try:
        result = finish(runtime, [runtime.delegate(REQUESTS[:1])[0]["handle"]])
        assert not result["findings"]
        assert "repository absence is unproven" in str(result["tasks"][0]["unknowns"])
        assert result["tasks"][0]["coverage"] == []
    finally:
        runtime.close()


def test_child_cannot_invent_source_observation_or_file_version(tmp_path):
    class ForgingClient:
        def complete_turn(self, request):
            raw = {
                "summary": "invented",
                "findings": [
                    {
                        "claim_key": "invented",
                        "statement": "Confirmed",
                        "path": "value.py",
                        "range": None,
                        "observation_id": "OBS-owned-by-somebody-else",
                        "file_hash": "fake",
                    }
                ],
                "unknowns": [],
            }
            return ModelTurnResult(
                text=json.dumps(raw), usage={"input_tokens": 10, "output_tokens": 10}
            )

    runtime = make_runtime(tmp_path, client_factory=lambda t: ForgingClient())
    try:
        result = finish(runtime, [runtime.delegate(REQUESTS[:1])[0]["handle"]])
        assert not result["findings"]
        assert result["tasks"][0]["status"] == "partial"
    finally:
        runtime.close()


def test_plan_revision_change_excludes_old_findings(tmp_path):
    from types import SimpleNamespace

    runtime = make_runtime(tmp_path)
    try:
        handle = runtime.delegate(REQUESTS[:1])[0]["handle"]
        assert finish(runtime, [handle])["findings"]
        runtime.plan_session = SimpleNamespace(
            plan=SimpleNamespace(plan_id="new", plan_version=2), local=threading.local()
        )
        task = runtime.collect([handle])["tasks"][0]
        assert task["status"] == "stale"
        assert task["validation"] == {"status": "stale", "reason": "plan_changed"}
        assert not runtime.collect([handle])["findings"]
    finally:
        runtime.close()


def test_worker_shares_plan_and_batch_permits_and_collect_wait_is_bounded(tmp_path):
    runtime = make_runtime(tmp_path)
    permits = read_permits(str(tmp_path), "run")
    assert permits.acquire() and permits.acquire()
    try:
        handles = [t["handle"] for t in runtime.delegate(REQUESTS)]
        assert all(t["status"] == "queued" for t in runtime.collect(handles, 20)["tasks"])
        assert not any(e["event_type"] == "subagent_model_started" for e in runtime.store.events())
    finally:
        permits.release()
        permits.release()
    try:
        assert all(t["status"] == "completed" for t in finish(runtime, handles)["tasks"])
    finally:
        runtime.close()


def test_storage_collision_rolls_back_the_entire_batch(tmp_path):
    import sqlite3

    from src.collaboration.contracts import TaskStatus

    runtime = make_runtime(tmp_path)
    try:
        handles = [t["handle"] for t in runtime.delegate(REQUESTS)]
        finish(runtime, handles)
        originals = runtime.tasks()
        tasks = copy.deepcopy(originals)
        tasks[0].task_id = "would-have-been-partially-inserted"
        for task in tasks:
            task.status = TaskStatus.READY
            task.payload["exploration"].update(status="queued", charged_tokens=24000)
        with pytest.raises(sqlite3.IntegrityError):
            runtime.store.submit_batch(tasks, run_token_limit=96000)
        assert not runtime.store.get_task(tasks[0].task_id)
        assert {t.task_id for t in runtime.tasks()} == {t.task_id for t in originals}
    finally:
        runtime.close()


@pytest.mark.parametrize("many", [False, True])
def test_hard_turn_and_tool_limits_are_enforced(tmp_path, many):
    class GreedyClient:
        def __init__(self):
            self.calls = 0

        def complete_turn(self, request):
            self.calls += 1
            tools = [
                ToolCall("read_file", {"path": "value.py"}, f"call-{self.calls}-{i}")
                for i in range(5 if many else 1)
            ]
            return ModelTurnResult(
                tool_calls=tools, usage={"input_tokens": 10, "output_tokens": 10}
            )

    model = GreedyClient()
    runtime = make_runtime(tmp_path, client_factory=lambda t: model)
    try:
        result = finish(runtime, [runtime.delegate(REQUESTS[:1])[0]["handle"]])
        usage = result["tasks"][0]["usage"]
        assert result["tasks"][0]["status"] == "partial"
        assert usage["model_turns"] <= 3 and usage["tool_calls"] <= 4
        assert model.calls == (1 if many else 3)
        assert not result["findings"]
    finally:
        runtime.close()
