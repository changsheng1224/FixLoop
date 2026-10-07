"""Replan integration: trusted failure facts, progress, and durable dedup."""

import io
import json

import pytest

from agent_runtime.plan_runtime.workspace import snapshot
from src.repair.plan_binding import RepairPlanBinding, ResumeRecoveryRequiredError
from src.repair.progress import ProgressEmitter
from src.state import VerificationResult
from tests.plan_l2_support import repair_fixture


@pytest.fixture
def failed_binding(tmp_path, request):
    orch, state, client = repair_fixture(tmp_path)
    events = []
    output = io.StringIO()
    orch._progress = ProgressEmitter(text_sink=output, record=events.append)
    before = orch._snapshot_repo()
    patches, meta = orch._run_patcher_toolized(state, "repair", {})
    assert patches, (meta, state.agent_errors)
    binding = orch._plan_binding
    state.node_timings["plan_verification_receipt"] = {
        "command": "controlled-verifier",
        "completed": True,
    }
    failure = getattr(request, "param", None) or VerificationResult(
        total_tests=1, failed=1, failure_logs=["AssertionError: actual output mismatch"]
    )
    binding.run_verifier(lambda: failure)
    orch._restore_repo_snapshot(before)
    state.retry_count = 1
    client._outputs.append(
        '{"conclusion":"Revise the hypothesis using the failure and current source."}'
    )
    yield binding, client, events, output
    if not binding._closed:
        binding.close()


@pytest.mark.parametrize("output_budget", [4096, 8192])
def test_replan_uses_role_output_budget(failed_binding, output_budget):
    binding, client, _, _ = failed_binding
    binding.agent.config.max_new_tokens = output_budget
    requested = []
    complete = client.complete

    def record(prompt, max_new_tokens, **kwargs):
        requested.append(max_new_tokens)
        return complete(prompt, max_new_tokens=max_new_tokens, **kwargs)

    client.complete = record
    binding._retry_from_verification()
    assert requested == [output_budget]
    assert binding.session.plan.plan_version == 2


def test_replan_prompt_progress_and_duplicate_commit(failed_binding):
    binding, client, events, output = failed_binding
    trigger = binding._verification_trigger()[0]
    original = binding.session.plan_view()
    before = client.session_usage["calls"]
    binding._retry_from_verification()
    assert client.session_usage["calls"] == before + 1
    payload = json.loads(client.prompts[-1].split("\n", 1)[1])["replan"]
    assert payload["old_plan"] == original
    with pytest.raises(ValueError, match="stale_or_modified"):
        binding.session.validate_plan_view(original)
    assert payload["trigger_ref"] == trigger
    assert "AssertionError" in payload["failure_excerpt"]
    assert payload["verification_receipt"]["completed"] is True
    assert payload["failed_workspace"] != payload["current_workspace"]
    assert payload["current_workspace"] == snapshot(binding.session.workspace)
    assert all(binding.session.evidence.valid(r) for r in payload["fresh_source_evidence_refs"])
    binding.sync()
    progress = binding.state.node_timings["plan_progress"]
    view = binding.session.plan_view()
    assert progress["state_revision"] == view["state_revision"]
    assert progress["plan_checksum"] == view["plan_checksum"]
    assert all({k: n[k] for k in v} == v for n, v in zip(progress["nodes"], view["nodes"]))
    starts = [
        e for e in events if e.extras.get("phase") == "replan" and "replan_started" in e.summary
    ]
    commits = [e for e in events if "replan_committed" in e.summary]
    assert len(starts) == len(commits) == 1
    assert starts[0].ts <= commits[0].ts
    assert "replan_started" in output.getvalue()
    assert {e.extras.get("phase") for e in events} >= {
        "explore",
        "analyze",
        "edit",
        "verify",
        "replan",
    }
    with pytest.raises(ValueError):
        binding._retry_from_verification()
    assert client.session_usage["calls"] == before + 1
    assert (
        binding.session.store.latest("replan_request", "trigger_ref")[trigger]["status"]
        == "committed"
    )


@pytest.mark.parametrize(
    "failed_binding,reason",
    [
        (
            VerificationResult(total_tests=1, failure_logs=["ModuleNotFoundError"]),
            "verification_env",
        ),
        (
            VerificationResult(failed=1, failure_logs=["AssertionError"]),
            "verification_empty_collection",
        ),
        (VerificationResult(total_tests=1, failure_logs=["unclassified"]), "verification_unknown"),
    ],
    indirect=["failed_binding"],
)
def test_non_code_failures_never_request_model(failed_binding, reason):
    binding, client, _, _ = failed_binding
    calls = client.session_usage["calls"]
    old = binding.session.plan
    with pytest.raises(ValueError, match="keep_plan:" + reason):
        binding._retry_from_verification()
    assert client.session_usage["calls"] == calls
    assert binding.session.plan == old
    assert not binding.session.store.latest("replan_request", "trigger_ref")


def test_invalid_candidate_keeps_old_plan_and_one_model_attempt_across_resume(failed_binding):
    binding, client, _, _ = failed_binding
    client._outputs[-1] = '{"conclusion":"invalid", "nodes": []}'
    old = binding.session.plan
    calls = client.session_usage["calls"]
    with pytest.raises(ValueError, match="candidate_rejected"):
        binding._retry_from_verification()
    assert binding.session.plan == old
    with pytest.raises(ValueError, match="already_processed:rejected"):
        binding._retry_from_verification()
    assert client.session_usage["calls"] == calls + 1
    binding.close()
    resumed = RepairPlanBinding(binding.orchestrator, binding.state)
    try:
        with pytest.raises(ValueError, match="already_processed:rejected"):
            resumed._retry_from_verification()
        assert resumed.session.plan == old
        assert client.session_usage["calls"] == calls + 1
    finally:
        resumed.close()


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("cancel", "replan_cancelled"),
        ("uncertain", "workspace_execution_uncertain"),
        ("rollback", "replan_rollback_unconfirmed"),
        ("resource", "replan_resource_cleanup_unconfirmed"),
        ("stop_loss", "stop_loss"),
        ("budget", "plan_generation_global_budget_exhausted"),
        ("deadline", "plan_generation_deadline_exceeded"),
    ],
)
def test_gates_block_before_model(failed_binding, mode, reason):
    binding, client, _, _ = failed_binding
    if mode == "cancel":
        from agent_runtime.cancellation import CancellationToken

        binding.agent.cancel_token = CancellationToken()
        binding.agent.cancel_token.cancel("test")
    elif mode == "uncertain":
        binding.agent.tool_context.execution_uncertain = True
    elif mode == "rollback":
        latest = [e["payload"] for e in binding.session.store.events() if e["kind"] == "rollback"][
            -1
        ]
        binding.session.store.append("rollback", {**latest, "completed": False})
    elif mode == "resource":
        binding.coordinator.register_resource(
            resource_id="unconfirmed-read", kind="exploration_task", effect="read"
        )
    elif mode == "stop_loss":
        binding.state.control.stop_loss = "no_progress"
    elif mode == "budget":
        binding.exploration.budget.restore({"limits": {"llm_calls": 1}, "used": {"llm_calls": 1}})
    else:
        from agent_runtime.repair_runtime import ExecutionDeadline

        binding.agent._repair_deadline = ExecutionDeadline.from_remaining(0)
    old = binding.session.plan
    calls = client.session_usage["calls"]
    with pytest.raises(ValueError, match=reason):
        binding._retry_from_verification()
    assert binding.session.plan == old
    assert client.session_usage["calls"] == calls


def test_stale_evidence_is_refetched_by_authorized_read(failed_binding):
    binding, client, events, _ = failed_binding
    session = binding.session
    for blob in {session.evidence.get(ref)["blob_ref"] for ref in binding._fresh_source_refs()}:
        (session.store.root / "blobs" / blob).unlink()
    assert not binding._fresh_source_refs()
    old_reads = len(session.store.latest("operation", "operation_id"))
    binding._retry_from_verification()
    assert len(session.store.latest("operation", "operation_id")) == old_reads + 1
    decisions = [e.extras["action"] for e in events if "replan_decided" in e.summary]
    assert decisions == ["needs_evidence", "replan"]
    payload = json.loads(client.prompts[-1].split("\n", 1)[1])["replan"]
    assert all(session.evidence.valid(r) for r in payload["fresh_source_evidence_refs"])


@pytest.mark.parametrize("mode", ["uncertain", "source"])
def test_safety_is_rechecked_after_model_before_commit(failed_binding, mode):
    binding, client, _, _ = failed_binding
    original = client.complete
    old = binding.session.plan

    def complete(*args, **kwargs):
        response = original(*args, **kwargs)
        if mode == "uncertain":
            binding.agent.tool_context.execution_uncertain = True
        else:
            from pathlib import Path

            (Path(binding.session.workspace) / "value.py").write_text("external change\n")
        return response

    client.complete = complete
    with pytest.raises(ValueError, match="uncertain|became_stale"):
        binding._retry_from_verification()
    assert binding.session.plan == old


def test_read_budget_denial_preserves_plan_before_evidence_refresh(failed_binding):
    binding, client, _, _ = failed_binding
    session = binding.session
    for blob in {session.evidence.get(r)["blob_ref"] for r in binding._fresh_source_refs()}:
        (session.store.root / "blobs" / blob).unlink()
    binding.read_budget.used = binding.read_budget.limit
    old = session.plan
    calls = client.session_usage["calls"]
    with pytest.raises(ValueError, match="read_budget_exceeded"):
        binding._retry_from_verification()
    assert session.plan == old
    assert client.session_usage["calls"] == calls


def test_unresolved_request_blocks_resume_without_model(failed_binding):
    binding, client, _, _ = failed_binding
    session = binding.session
    trigger = binding._verification_trigger()[0]
    session.store.append(
        "replan_request",
        {
            "trigger_ref": trigger,
            "plan_version": session.plan.plan_version,
            "plan_checksum": session.plan.plan_checksum,
            "status": "started",
        },
    )
    calls = client.session_usage["calls"]
    binding.close()
    with pytest.raises(ResumeRecoveryRequiredError, match="replan_attempt_outcome_unconfirmed"):
        RepairPlanBinding(binding.orchestrator, binding.state)
    assert client.session_usage["calls"] == calls


def test_commit_before_trace_is_reconciled_from_saved_plan(failed_binding):
    binding, client, _, _ = failed_binding
    trigger = binding._verification_trigger()[0]

    def crash(point):
        if point == "plan_saved" and binding.session.plan.plan_version == 2:
            raise SystemExit("commit cut")

    binding.session.fault = crash
    with pytest.raises(SystemExit, match="commit cut"):
        binding._retry_from_verification()
    calls = client.session_usage["calls"]
    binding.session.fault = None
    binding.close()
    resumed = RepairPlanBinding(binding.orchestrator, binding.state)
    try:
        assert resumed.session.plan.plan_version == 2
        assert (
            resumed.session.store.latest("replan_request", "trigger_ref")[trigger]["status"]
            == "committed"
        )
        assert client.session_usage["calls"] == calls
        assert (
            len(
                [
                    e
                    for e in resumed.session.store.events()
                    if e["kind"] == "trace"
                    and e["payload"].get("event") == "replan_committed"
                    and e["payload"].get("trigger_ref") == trigger
                ]
            )
            == 1
        )
    finally:
        resumed.close()
