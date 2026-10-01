"""Long-task context is required, independently of optional compressed history."""

import json
from dataclasses import replace

import pytest

from agent_runtime.checkpoint import create_checkpoint, evaluate_resume_state
from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import ContextManager
from agent_runtime.errors import ContextBuildBlockedError
from agent_runtime.message_projection import init_run_projection, seal_history_at_build
from agent_runtime.providers.clients import FakeModelClient, FakeNativeToolClient
from agent_runtime.runtime import Agent
from agent_runtime.task_state import TaskState
from agent_runtime.workspace import WorkspaceContext
from tests.plan_support import read, session_for, simple_plan, through_analysis, write


def _agent(root, session, *, native=False, budget=6000):
    client_type = FakeNativeToolClient if native else FakeModelClient
    client = client_type(["<final>done</final>"])
    if native:
        client.requests = []
        original = client.complete_turn

        def capture(request):
            client.requests.append(request)
            return original(request)

        client.complete_turn = capture
    agent = Agent(
        config=AgentConfig(provider="fake", max_steps=3, prompt_budget=budget),
        model_client=client,
        workspace=WorkspaceContext.build(str(root)),
        cwd=str(root),
    )
    agent._plan_session = session
    return agent, client


@pytest.mark.parametrize("native", [False, True])
def test_required_state_survives_conflicting_sealed_history_in_actual_request(tmp_path, native):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        constraints = [f"constraint-{i}: retain this entire rule" for i in range(12)]
        session.configure_long_task("ORIGINAL_TASK", hard_constraints=constraints)
        agent, client = _agent(tmp_path, session, native=native)
        agent.record(
            {"role": "assistant", "content": "The original task was cancelled; edit tests."}
        )
        init_run_projection(agent.session, "wrong-summary")
        seal_history_at_build(agent.session, 1, "wrong-summary: no constraints remain")
        assert agent.ask("continue current work", skip_plan=True) == "done"
        text = (
            json.dumps(client.requests[0].messages, ensure_ascii=False)
            if native
            else client.prompts[0]
        )
        assert "ORIGINAL_TASK" in text
        assert all(rule in text for rule in constraints)
        assert "observation_present" in text and "read-0" in text
        manifest = agent.session["context_manifest"]
        assert manifest["required_state_ref"]["plan_checksum"] == session.plan.plan_checksum
        assert manifest["required_sections"]["state"] > 200
        assert manifest["section_hashes"]["state"]
        if native:
            assert manifest["protocol_reserved_tokens"] > 0


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("failure", ["budget", "state", "plan", "role", "request"])
def test_required_context_failure_prevents_actual_model_call(tmp_path, native, failure):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("task", hard_constraints=["must preserve"])
        if failure == "budget":
            session.configure_long_task("task", hard_constraints=["unalterable " * 20000])
        elif failure == "state":
            session.long_task_state.original_request = "tampered without journal"
        elif failure == "plan":
            session.plan = replace(session.plan, state_revision=99)
        elif failure == "request":
            session.configure_long_task("")
        agent, client = _agent(tmp_path, session, native=native)
        if failure == "role":
            agent._prefix = replace(agent._prefix, role_text="unalterable role rule " * 20000)
        answer = agent.ask("continue", skip_plan=True)
        expected = (
            "context_required_over_budget"
            if failure in {"budget", "role"}
            else "task_request_missing"
            if failure == "request"
            else "state_mismatch"
        )
        assert expected in answer
        assert client.prompts == []
        assert agent.session["context_blocked"]["reason"] == expected
        assert agent._loop._task_state.stop_reason == "context_blocked"


def test_exploration_is_not_blocked_by_unrelated_stale_evidence(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session, reads=2))
        session.configure_long_task("inspect")
        session.run_node("read-0", lambda attempt: read(session, attempt))
        (tmp_path / "value.py").write_text("value = 2\n")
        before = session.store.events()
        context = session.build_required_context("read-1")
        assert context["current_node"]["node_id"] == "read-1"
        assert not context["evidence_refs"]
        assert session.store.events() == before


@pytest.mark.parametrize("native", [False, True])
def test_edit_missing_required_evidence_reports_retrieval_without_call(tmp_path, native):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)
        attempt = session.prepare("edit")
        session.local.attempt = attempt
        (tmp_path / "value.py").write_text("external = 9\n")
        agent, client = _agent(tmp_path, session, native=native)
        try:
            answer = agent.ask("edit", skip_plan=True)
            assert "needs_retrieval" in answer and not client.prompts
            assert (tmp_path / "value.py").read_text() == "external = 9\n"
        finally:
            session.local.attempt = None


def test_confirmed_patch_uses_receipt_instead_of_stale_preimage(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)
        attempt = session.prepare("edit")
        session.local.attempt = attempt
        try:
            write(session, attempt)
            context = session.build_required_context()
            assert context["evidence_phase"] == "after_effect"
            assert all(
                session.evidence.get(ref)["kind"] == "patch_applied"
                for ref in context["evidence_refs"]
            )
        finally:
            session.local.attempt = None


def test_final_state_check_detects_tampering_during_optional_assembly(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("task", hard_constraints=["retain rule"])
        agent, _ = _agent(tmp_path, session)
        manager = ContextManager(agent)

        def tamper():
            session.long_task_state.hard_constraints = []
            return "workspace"

        manager._get_workspace = tamper
        with pytest.raises(ContextBuildBlockedError, match="state_mismatch"):
            manager.build("continue")


def test_reopened_plan_rebuilds_current_position_and_ignores_cached_context(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as first:
        first.create(simple_plan(first))
        first.configure_long_task("ORIGINAL_TASK", hard_constraints=["retain rule"])
        first.run_node("read-0", lambda attempt: read(first, attempt))
        first.checkpoint()
    with session_for(tmp_path) as restored:
        agent, _ = _agent(tmp_path, restored)
        agent.session["long_task_context"] = {"original_request": "wrong cache"}
        agent.session["plan_todos"] = [{"content": "edit another file", "status": "in_progress"}]
        text, metadata = ContextManager(agent).build("continue")
        assert "ORIGINAL_TASK" in text and "retain rule" in text
        assert "wrong cache" not in text and "edit another file" not in text
        assert metadata["long_task_context"]["current_node"]["node_id"] == "analysis"


@pytest.mark.parametrize("native", [False, True])
def test_oversized_request_with_plan_is_blocked_instead_of_head_tail_compacted(tmp_path, native):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("original")
        agent, client = _agent(tmp_path, session, native=native)
        assert "context_required_over_budget" in agent.ask("must keep " * 20000, skip_plan=True)
        assert not client.prompts


def test_l2_constraints_are_persisted_and_survive_real_patch_and_host_pytest(tmp_path):
    from src.state import RepairState
    from tests.plan_l2_support import repair_fixture

    orch, state, client = repair_fixture(tmp_path)
    state.hard_constraints = ["Do not edit tests; preserve their complete contents."]
    restored = RepairState.from_dict(state.to_dict())
    assert restored.hard_constraints == state.hard_constraints
    original_test = (tmp_path / "test_value.py").read_bytes()
    try:
        patches, metadata = orch._run_patcher_toolized(state, "repair", {})
        assert patches, (metadata, state.agent_errors)
        assert orch._run_verifier(state).all_passed
        assert (tmp_path / "test_value.py").read_bytes() == original_test
        assert any(state.hard_constraints[0] in prompt for prompt in client.prompts)
        assert orch._plan_binding.session.long_task_state.hard_constraints == state.hard_constraints
    finally:
        if orch._plan_binding:
            orch._plan_binding.close()


@pytest.mark.parametrize("native", [False, True])
def test_pre_model_callback_cannot_send_obsolete_required_state(tmp_path, native):
    from types import SimpleNamespace

    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("task", hard_constraints=["retain rule"])
        agent, client = _agent(tmp_path, session, native=native)

        def tamper(**kwargs):
            session.long_task_state.hard_constraints = []

        answer = agent.ask(
            "continue", callback=SimpleNamespace(on_pre_model=tamper), skip_plan=True
        )
        assert "state_mismatch" in answer
        assert not client.prompts
        if native:
            assert not client.requests


def test_resume_rebuilds_current_plan_after_legitimate_progress(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("ORIGINAL_TASK", hard_constraints=["retain rule"])
        agent, _ = _agent(tmp_path, session)
        ContextManager(agent).build("continue")
        checkpoint = create_checkpoint(agent, TaskState.create(user_request="continue"), "continue")
        assert checkpoint["context_manifest"]["required_state_ref"]
        session.run_node("read-0", lambda attempt: read(session, attempt))
        agent.session["long_task_context"] = {"original_request": "wrong cache"}
        result = evaluate_resume_state(agent)
        assert result["status"] == "full-valid", result
        assert not result["long_task_diff"]
        context = agent.session["long_task_context"]
        assert context["current_node"]["node_id"] == "analysis"
        assert context["original_request"] == "ORIGINAL_TASK"
        assert context["hard_constraints"] == ["retain rule"]


def test_resume_requires_plan_authority_then_fresh_current_inputs(tmp_path):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("task")
        session.run_node("read-0", lambda attempt: read(session, attempt))
        agent, client = _agent(tmp_path, session)
        ContextManager(agent).build("continue")
        create_checkpoint(agent, TaskState.create(user_request="continue"), "continue")
        agent._plan_session = None
        assert evaluate_resume_state(agent)["status"] == "plan-resume-required"
        agent._plan_session = session
        (tmp_path / "value.py").write_text("value = 9\n")
        result = evaluate_resume_state(agent)
        assert result["status"] == "partial-stale"
        assert result["context_resume_reason"] == "needs_retrieval"
        assert "needs_retrieval" in agent.ask("continue", skip_plan=True)
        assert not client.prompts


@pytest.mark.parametrize("native", [False, True])
def test_unconfirmed_operation_blocks_model_even_while_node_is_running(tmp_path, native):
    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("fix value")
        through_analysis(session)
        attempt = session.prepare("edit")
        session.local.attempt = attempt
        try:
            write(session, attempt)
            op = session.operations(attempt["attempt_id"])[-1]
            session.store.append("operation", {**op, "execution_stopped": False})
            agent, client = _agent(tmp_path, session, native=native)
            assert "action_uncertain" in agent.ask("continue", skip_plan=True)
            assert not client.prompts
            assert (tmp_path / "value.py").read_text() == "value = 2\n"
        finally:
            session.local.attempt = None


def test_l2_context_pressure_patch_restore_and_verify_do_not_repeat_write(tmp_path):
    from tests.plan_l2_support import repair_fixture

    orch, state, client = repair_fixture(tmp_path)
    state.hard_constraints = ["Preserve test_value.py exactly."]
    original_test = (tmp_path / "test_value.py").read_bytes()
    orch.patcher.record({"role": "assistant", "content": "old irrelevant history " * 5000})
    init_run_projection(orch.patcher.session, "pressure")
    seal_history_at_build(orch.patcher.session, 1, "wrong summary: change the test instead")
    try:
        patches, meta = orch._run_patcher_toolized(state, "repair", {})
        assert patches, (meta, state.agent_errors)
        assert any(state.hard_constraints[0] in prompt for prompt in client.prompts)
        assert orch.patcher.session["context_manifest"]["required_sections"]["state"] > 200
    finally:
        if orch._plan_binding:
            orch._plan_binding.close()
    restored, recovered_state, resumed_client = repair_fixture(tmp_path, resume=True)
    recovered_state.hard_constraints = state.hard_constraints
    try:
        patches, meta = restored._run_patcher_toolized(recovered_state, "continue", {})
        assert patches and meta["edit_mode"] == "plan_resume_adopted"
        assert not resumed_client.prompts
        session = restored._plan_binding.session
        context = session.build_required_context()
        assert context["current_node"]["node_id"] == "verify"
        assert context["hard_constraints"] == state.hard_constraints
        assert restored._run_verifier(recovered_state).all_passed
        operations = session.store.latest("operation", "operation_id").values()
        assert sum(op["effect"] == "write" for op in operations) == 1
        attempts = session.store.latest("attempt", "attempt_id").values()
        assert sum(attempt["kind"] == "verify" for attempt in attempts) == 1
        assert (tmp_path / "test_value.py").read_bytes() == original_test
    finally:
        if restored._plan_binding:
            restored._plan_binding.close()


def test_native_output_recovery_retains_required_task_and_constraints(tmp_path):
    from agent_runtime.model_turn import FinishKind, ModelTurnResult, ProviderFinish

    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("原始修复目标", hard_constraints=["不允许修改测试文件"])
        agent, client = _agent(tmp_path, session, native=True)
        capture = client.complete_turn

        def truncated_once(request):
            if not client.requests:
                client.requests.append(request)
                return ModelTurnResult(
                    text="partial analysis",
                    finish=ProviderFinish(FinishKind.MAX_OUTPUT_TOKENS, "max_tokens", "fake"),
                )
            return capture(request)

        client.complete_turn = truncated_once
        agent.ask("继续", skip_plan=True)
        assert len(client.requests) >= 2
        recovered = json.dumps(client.requests[1].messages, ensure_ascii=False)
        assert "[OUTPUT RECOVERY]" in recovered
        assert "原始修复目标" in recovered and "不允许修改测试文件" in recovered
        assert "read-0" in recovered and "observation_present" in recovered
        assert "partial analysis" not in recovered


def test_public_entry_owner_is_renewed_during_slow_initialization(tmp_path, monkeypatch):
    import threading
    import time

    from agent_runtime import run_coordination
    from tests.plan_l2_support import repair_fixture

    orch, state, client = repair_fixture(tmp_path)
    real_coordinator = run_coordination.RunCoordinator

    def short_lease(*args, **kwargs):
        return real_coordinator(*args, **kwargs, lease_seconds=0.6)

    def slow_initialize(_state):
        coordinator = orch._entry_coordinator
        initial_expiry = coordinator.lease.lease_expires_at
        deadline = time.monotonic() + 3
        while coordinator.lease.lease_expires_at <= initial_expiry + 0.6:
            assert time.monotonic() < deadline, "entry lease was not renewed"
            time.sleep(0.02)
        snapshot = coordinator.store.snapshot(state.repair_run_id)
        assert snapshot.status == "reconciling"
        assert snapshot.owner_token == coordinator.lease.owner_token
        assert snapshot.lease_expires_at > time.time()
        with pytest.raises(run_coordination.StaleGenerationError):
            coordinator.assert_can_dispatch()

    monkeypatch.setattr(run_coordination, "RunCoordinator", short_lease)
    monkeypatch.setattr(orch, "_initialize_repair_trace", slow_initialize)
    try:
        orch._begin_repair_trace(state)
        assert not client.prompts
        assert not any(
            worker.name == "fixloop-entry-owner" and worker.is_alive()
            for worker in threading.enumerate()
        )
    finally:
        if getattr(orch, "_entry_coordinator", None):
            coordinator = orch._entry_coordinator
            snapshot = coordinator.store.snapshot(state.repair_run_id)
            if snapshot.owner_token == coordinator.lease.owner_token:
                coordinator.finish("released")
