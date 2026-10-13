"""Run-wide model dispatch accounting and conservative recovery contracts."""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_runtime.budget_manager import BudgetManager
from agent_runtime.cancellation import BlockingDeadlineError, CancellationToken, CancelledError
from agent_runtime.model_call_budget import (
    ModelBudgetExceededError,
    complete_text,
    invoke_model_call,
)
from agent_runtime.model_turn import ModelTurnResult
from agent_runtime.repair_runtime import ExecutionDeadline


def call(budget, result=None, **kwargs):
    client = kwargs.pop("client", SimpleNamespace())
    return invoke_model_call(
        kwargs.pop("fn", lambda: result),
        budget=budget,
        client=client,
        input_tokens=20,
        output_tokens=10,
        purpose=kwargs.pop("purpose", "repair"),
        **kwargs,
    )


def test_incurred_usage_is_recorded_even_above_limit():
    budget = BudgetManager({"total_tokens": 10})
    budget.record("total_tokens", 15)
    assert budget.summary()["total_tokens"]["used"] == 15
    assert not budget.check("total_tokens").allowed


def test_durable_reservation_callback_runs_before_provider_dispatch():
    budget, events = (BudgetManager(), [])

    def reserved(snapshot):
        assert snapshot["used"]["llm_calls"] == 1
        assert next(iter(snapshot["model_calls"].values()))["usage_status"] == "unknown"
        events.append("saved")

    call(budget, fn=lambda: events.append("dispatched"), on_reserved=reserved)
    assert events == ["saved", "dispatched"]


def test_failed_reservation_persistence_prevents_dispatch_and_keeps_debt():
    budget, dispatched = (BudgetManager(), [])

    def fail(snapshot):
        raise ValueError("owner_lost")

    with pytest.raises(ValueError, match="owner_lost"):
        call(budget, fn=lambda: dispatched.append(True), on_reserved=fail)
    assert not dispatched and budget.summary()["llm_calls"]["used"] == 1
    assert next(iter(budget.snapshot()["model_calls"].values()))["usage_status"] == "unknown"


def test_auxiliary_recovery_merges_only_missing_calls_without_refunding_limits():
    source = BudgetManager()
    call(source, purpose="extraction")
    call(source, purpose="planning")
    budget = BudgetManager({"llm_calls": 1})
    call(budget, purpose="repair")
    budget.restore_missing_model_calls(source.snapshot(), purpose="extraction")
    budget.restore_missing_model_calls(source.snapshot(), purpose="extraction")
    assert budget.summary()["llm_calls"]["used"] == 2
    assert budget.summary()["llm_calls"]["limit"] == 1
    assert budget.summary()["total_tokens"]["used"] == 60
    assert {c["purpose"] for c in budget.snapshot()["model_calls"].values()} == {
        "repair",
        "extraction",
    }


def test_reservation_is_atomic_and_never_dispatches_over_budget():
    budget = BudgetManager({"llm_calls": 1, "total_tokens": 29})
    dispatched = []
    with pytest.raises(ModelBudgetExceededError):
        call(budget, fn=lambda: dispatched.append(True))
    assert not dispatched
    assert budget.summary()["llm_calls"]["used"] == 0
    assert not budget.snapshot()["scopes"]


def test_actual_overrun_is_settled_and_stops_following_call():
    budget = BudgetManager({"total_tokens": 30})
    call(budget, ModelTurnResult(usage={"input_tokens": 25, "output_tokens": 15}))
    assert budget.summary()["total_tokens"]["used"] == 40
    assert budget.summary()["prompt_tokens"]["used"] == 25
    assert budget.summary()["output_tokens"]["used"] == 15
    assert next(iter(budget.snapshot()["model_calls"].values()))["usage_status"] == "actual"
    with pytest.raises(ModelBudgetExceededError):
        call(budget)


def test_unknown_usage_and_purpose_survive_restore():
    budget = BudgetManager({"llm_calls": 1})
    call(budget, "no usage", purpose="summary")
    restored = BudgetManager()
    restored.restore(budget.snapshot())
    assert restored.summary()["total_tokens"]["used"] == 30
    assert list(restored.snapshot()["model_calls"].values()) == [
        {"purpose": "summary", "usage_status": "estimated"}
    ]
    with pytest.raises(ModelBudgetExceededError):
        call(restored)


def test_cancelled_or_expired_before_call_consumes_nothing():
    budget = BudgetManager()
    token = CancellationToken()
    token.cancel()
    with pytest.raises(CancelledError):
        call(budget, cancel_token=token)
    with pytest.raises(TimeoutError):
        call(budget, deadline=ExecutionDeadline.from_remaining(0))
    assert not budget.snapshot()["model_calls"]


@pytest.mark.parametrize("cancel", [True, False])
def test_interrupted_call_and_late_result_cannot_refund_reservation(cancel):
    entered, release, done = (threading.Event(), threading.Event(), threading.Event())
    budget, token = (BudgetManager(), CancellationToken())

    def provider():
        entered.set()
        assert release.wait(2)
        done.set()
        return ModelTurnResult(usage={"input_tokens": 1, "output_tokens": 1})

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call, budget, fn=provider, cancel_token=token, timeout_s=0.15)
        try:
            assert entered.wait(1)
            if cancel:
                token.cancel()
            with pytest.raises(CancelledError if cancel else BlockingDeadlineError):
                future.result(1)
            restored = BudgetManager()
            restored.restore(budget.snapshot())
        finally:
            release.set()
        assert done.wait(1)
    assert budget.summary()["total_tokens"]["used"] == 30
    assert restored.summary()["total_tokens"]["used"] == 30
    assert next(iter(budget.snapshot()["model_calls"].values()))["usage_status"] == "unknown"


def test_shared_client_reads_fresh_usage_and_ignores_stale_last_usage():
    client = SimpleNamespace(last_usage={"input_tokens": 1, "output_tokens": 1})
    budget = BudgetManager()
    call(budget, "text", client=client)
    assert budget.summary()["total_tokens"]["used"] == 30
    client.session_usage = {"calls": 3, "input_tokens": 200, "output_tokens": 100}

    def provider():
        client.session_usage.update(calls=4, input_tokens=205, output_tokens=102)
        return "fresh"

    call(budget, client=client, fn=provider)
    assert budget.summary()["total_tokens"]["used"] == 37


def test_competing_dispatches_share_one_atomic_call_limit():
    budget = BudgetManager({"llm_calls": 1})

    def dispatch(_):
        try:
            call(budget, ModelTurnResult(usage={"input_tokens": 1, "output_tokens": 1}))
            return True
        except ModelBudgetExceededError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(dispatch, range(8))) == 1
    assert budget.summary()["llm_calls"]["used"] == 1


def test_auxiliary_call_uses_same_ledger_and_output_limit():
    from agent_runtime.config import AgentConfig
    from agent_runtime.providers.clients import FakeModelClient

    budget = BudgetManager({"output_tokens": 9})
    client = FakeModelClient(["should not dispatch"])
    agent = SimpleNamespace(config=AgentConfig(), session={}, budget_manager=budget)
    with pytest.raises(ModelBudgetExceededError):
        complete_text(agent, client, "summarize", max_output_tokens=10, purpose="summary")
    assert not client.prompts
    assert agent.session["runtime_budget"]["used"]["output_tokens"] == 0


def test_input_window_is_independent_of_cumulative_budget():
    from agent_runtime.config import AgentConfig

    config = AgentConfig(model_context_window=4096, context_safety_margin=128)
    assert config.input_window(1024) == 2944
    assert config.effective_budget()["prompt_tokens"] == 100000
    with pytest.raises(ValueError, match="model_context_window_exhausted"):
        config.input_window(4096)


def test_parent_scope_settlement_is_idempotent_and_directional():
    budget = BudgetManager()
    cap = {"prompt_tokens": 100, "output_tokens": 100, "total_tokens": 100, "llm_calls": 3}
    assert budget.reserve_scope("child", cap)
    assert budget.reserve_scope("child", cap)
    actual = {"prompt_tokens": 20, "output_tokens": 10, "total_tokens": 30, "llm_calls": 1}
    budget.reconcile_scope("child", actual)
    budget.reconcile_scope("child", actual)
    assert budget.summary()["total_tokens"]["used"] == 30
    assert budget.summary()["prompt_tokens"]["used"] == 20


def test_xml_empty_retries_each_reserve_one_call(tmp_path):
    from agent_runtime.config import AgentConfig
    from agent_runtime.providers.clients import FakeModelClient
    from agent_runtime.runtime import Agent
    from agent_runtime.workspace import WorkspaceContext

    client = FakeModelClient(["", "", "<final>should not dispatch</final>"])
    agent = Agent(
        AgentConfig(provider="fake", budget={"max_llm_calls": 2}),
        client,
        WorkspaceContext.build(str(tmp_path)),
        cwd=str(tmp_path),
    )
    answer = agent.ask("inspect", skip_plan=True)
    assert "预算已耗尽" in answer
    assert len(client.prompts) == 2
    assert agent.session["runtime_budget"]["used"]["llm_calls"] == 2


def test_exploration_receipts_settle_input_and_output_separately(tmp_path):
    from tests.test_exploration_runtime import REQUESTS, finish, make_runtime

    runtime = make_runtime(tmp_path)
    try:
        handles = [t["handle"] for t in runtime.delegate(REQUESTS[:1])]
        finish(runtime, handles)
        runtime.sync_budget()
        runtime.sync_budget()
        used = runtime.budget.snapshot()["used"]
        assert used["prompt_tokens"] == 200
        assert used["output_tokens"] == 100
        assert used["total_tokens"] == 300
        assert used["llm_calls"] == 2
    finally:
        runtime.close()


def test_repair_roles_and_resume_share_budget_and_deadline():
    from agent_runtime.config import AgentConfig
    from src.repair.session import ExecutionSession
    from src.state import RepairState

    owner = object.__new__(ExecutionSession)
    owner.patcher = SimpleNamespace(config=AgentConfig())
    from src.repair.execution.lifecycle import ExecutionLifecycle

    owner.lifecycle = ExecutionLifecycle(owner)
    from src.repair.verification.execution_policy import VerificationBackend

    owner.verification_backend = VerificationBackend()
    owner.lifecycle.initialize_model_budget(None, 60)
    assert owner.patcher.budget_manager is owner.model_budget
    assert owner.patcher.execution_deadline is owner.model_deadline
    call(owner.model_budget, "unknown", purpose="planning")
    state = RepairState(issue_input="repair")
    owner.lifecycle.release_model_budget(state)
    assert owner.patcher.budget_manager is None
    owner.lifecycle.initialize_model_budget(state.to_dict(), 90)
    try:
        assert owner.model_budget.summary()["total_tokens"]["used"] == 30
        assert owner.model_deadline.remaining_s() <= 60
        assert not hasattr(owner.verification_backend, "budget_manager")
    finally:
        owner.lifecycle.release_model_budget(state)


def test_intent_retries_consume_shared_budget():
    from agent_runtime.intent.llm_runtime import IntentLlmPolicy, IntentLlmRuntime

    class FailingClient:
        calls = 0

        def complete(self, prompt, max_new_tokens):
            self.calls += 1
            raise RuntimeError("provider unavailable")

    client, budget = (FailingClient(), BudgetManager({"llm_calls": 1}))
    result = IntentLlmRuntime(IntentLlmPolicy(max_retries=2, retry_backoff_s=0)).complete(
        client, "classify", budget=budget
    )
    assert result.reason == "budget_exceeded"
    assert client.calls == 1
    assert budget.summary()["total_tokens"]["used"] > 384


def test_auxiliary_expired_deadline_prevents_dispatch():
    from agent_runtime.config import AgentConfig
    from agent_runtime.providers.clients import FakeModelClient

    client = FakeModelClient(["should not dispatch"])
    agent = SimpleNamespace(
        config=AgentConfig(), session={}, execution_deadline=ExecutionDeadline.from_remaining(0)
    )
    with pytest.raises(TimeoutError):
        complete_text(agent, client, "summarize", max_output_tokens=10, purpose="summary")
    assert not client.prompts
    assert not agent.session["runtime_budget"]["model_calls"]


@pytest.mark.parametrize("native", [False, True])
def test_plan_request_uses_model_window_minus_output_and_margin(tmp_path, native):
    from agent_runtime.context_manager import ContextManager
    from tests.plan_support import session_for, simple_plan
    from tests.test_required_long_task_context import _agent

    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("ORIGINAL", hard_constraints=["preserve tests"])
        agent, _ = _agent(tmp_path, session, native=native)
        agent.config.model_context_window = 5800
        agent.config.context_safety_margin = 100
        request, metadata = ContextManager(agent).prepare_request(
            "continue", protocol="native" if native else "xml", max_output_tokens=512
        )
        assert metadata["provider_input_tokens"] <= 5188
        assert "ORIGINAL" in str(request.messages)
        assert "preserve tests" in str(request.messages)
        assert metadata["required_state_ref"]


def test_required_request_over_model_window_blocks_without_dispatch(tmp_path):
    from agent_runtime.context_manager import ContextManager
    from agent_runtime.errors import ContextBuildBlockedError
    from tests.plan_support import session_for, simple_plan
    from tests.test_required_long_task_context import _agent

    (tmp_path / "value.py").write_text("value = 1\n")
    with session_for(tmp_path) as session:
        session.create(simple_plan(session))
        session.configure_long_task("required goal " * 1000)
        agent, client = _agent(tmp_path, session)
        agent.config.model_context_window = 2000
        with pytest.raises(ContextBuildBlockedError, match="context_required_over_budget"):
            ContextManager(agent).prepare_request("continue", protocol="xml", max_output_tokens=512)
        assert not client.prompts


def test_explicit_parent_budget_is_not_reset_by_ask(tmp_path):
    from agent_runtime.config import AgentConfig
    from agent_runtime.providers.clients import FakeModelClient
    from agent_runtime.runtime import Agent
    from agent_runtime.workspace import WorkspaceContext

    client = FakeModelClient(["<final>should not dispatch</final>"])
    agent = Agent(
        AgentConfig(provider="fake"),
        client,
        WorkspaceContext.build(str(tmp_path)),
        cwd=str(tmp_path),
    )
    budget = BudgetManager({"llm_calls": 1})
    budget.reserve("llm_calls")
    agent.budget_manager = budget
    assert "预算已耗尽" in agent.ask("inspect", skip_plan=True)
    assert agent.budget_manager is budget
    assert not client.prompts


def test_budget_fields_load_from_environment(tmp_path, monkeypatch):
    from agent_runtime.config_loader import load_runtime_policy

    monkeypatch.setenv("FIXLOOP_MODEL_CONTEXT_WINDOW", "16384")
    monkeypatch.setenv("FIXLOOP_CONTEXT_SAFETY_MARGIN", "512")
    monkeypatch.setenv("FIXLOOP_BUDGET_OUTPUT_TOKENS", "4000")
    monkeypatch.setenv("FIXLOOP_BUDGET_TOTAL_TOKENS", "12000")
    config = load_runtime_policy(workspace_root=str(tmp_path))
    assert config.model_context_window == 16384
    assert config.context_safety_margin == 512
    assert config.effective_budget()["output_tokens"] == 4000
    assert config.effective_budget()["total_tokens"] == 12000


def test_role_checkpoint_cannot_refund_newer_run_budget_or_extend_deadline(tmp_path):
    from agent_runtime.agent_loop import AgentLoop
    from agent_runtime.config import AgentConfig
    from agent_runtime.providers.clients import FakeModelClient
    from agent_runtime.runtime import Agent
    from agent_runtime.workspace import WorkspaceContext

    agent = Agent(
        AgentConfig(provider="fake"),
        FakeModelClient([]),
        WorkspaceContext.build(str(tmp_path)),
        cwd=str(tmp_path),
    )
    budget = BudgetManager()
    older = budget.snapshot()
    call(budget, "unknown", purpose="planning")
    agent.budget_manager = budget
    agent.execution_deadline = ExecutionDeadline(20)
    agent.budget_run_bound = True
    loop = AgentLoop(agent)
    loop._restore_runtime_control({"budget_manager": older, "deadline": {"remaining_s": None}})
    assert budget.summary()["total_tokens"]["used"] == 30
    assert agent.execution_deadline.remaining_s() <= 20


def test_native_provider_missing_usage_keeps_reservation(monkeypatch):
    from agent_runtime.model_timing import ModelCallTiming
    from agent_runtime.model_turn import ModelTurnRequest
    from agent_runtime.providers.clients import AnthropicCompatibleModelClient

    client = AnthropicCompatibleModelClient("test", "https://unused.invalid", "test-key")
    client._record_usage({"input_tokens": 5, "output_tokens": 2})
    monkeypatch.setattr(
        client,
        "_post_messages",
        lambda *a, **kw: (
            {"content": [{"type": "text", "text": "hello"}], "stop_reason": "end_turn"},
            ModelCallTiming(ttft_ms=0, total_ms=0),
        ),
    )
    budget = BudgetManager()
    call(
        budget,
        client=client,
        fn=lambda: client.complete_turn(
            ModelTurnRequest("", [{"role": "user", "content": "prompt"}], max_output_tokens=10)
        ),
    )
    assert budget.summary()["total_tokens"]["used"] == 30
    assert next(iter(budget.snapshot()["model_calls"].values()))["usage_status"] == "estimated"
