"""Independent kernel services: owner commits, durable refs and cleanup failures."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agent_runtime.cancellation import CancellationToken
from agent_runtime.config import AgentConfig
from agent_runtime.context_runtime import ObservationStore
from agent_runtime.model_turn import ToolCall
from agent_runtime.observation_recording import ToolObservationRecorder
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.repair_runtime import CanonicalToolCall
from agent_runtime.runtime import Agent
from agent_runtime.tool_batch_runtime import BatchExecutionHooks, SettledToolStep, ToolBatchRunner
from agent_runtime.tool_result import ToolResult
from agent_runtime.turn_progress import TurnEventEmitter
from agent_runtime.workspace import WorkspaceContext


def independent_runtime(tmp_path, *, cancel=None, fail_settle=False):
    (tmp_path / "value.py").write_text("value = 1\nother = 2\n")
    agent = Agent(
        AgentConfig(provider="fake", approval="auto", loop_detect_threshold=0),
        FakeModelClient([]),
        WorkspaceContext.build(str(tmp_path)),
        cwd=str(tmp_path),
    )
    owner = threading.get_ident()
    log, events, active, steps_closed, operations_closed = [], [], [], [], []
    recorder = ToolObservationRecorder(
        session=agent.session,
        root=str(tmp_path),
        state_root="",
        emit=lambda kind, payload: events.append((kind, payload)),
        on_changed_paths=lambda name: None,
        on_retrieval=lambda *args: None,
    )
    progress = TurnEventEmitter(
        "isolated-run", "turn", lambda kind, payload: events.append((kind, payload))
    )

    def step_flow(call, prepared):
        log.append(("prepare", threading.get_ident(), call.call_id))
        try:
            result = yield prepared
            log.append(("record", threading.get_ident(), call.call_id))
            if fail_settle:
                raise OSError("observation storage failed")
            recorded = recorder.record(
                CanonicalToolCall.create(call.tool_name, call.arguments, call_id=call.call_id),
                result,
                duration_ms=result.duration_ms,
                metadata=result.metadata,
                source_version="",
                idempotency_key=call.context.idempotency_key,
                call_context=call.context,
            )
            assert result.metadata["observation_id"] == recorded.stored.observation_id
            return result.content
        finally:
            steps_closed.append(call.call_id)

    def settle_step(flow, result):
        try:
            flow.send(result)
        except StopIteration as done:
            return SettledToolStep(done.value, result.metadata["observation_id"])
        raise AssertionError("unexpected second yield")

    def operation(call):
        log.append(("plan_prepare", threading.get_ident(), call.call_id))
        try:
            result = yield
            log.append(("plan_commit", threading.get_ident(), call.call_id))
            return result
        finally:
            operations_closed.append(call.call_id)

    def dispatch(name, gated):
        log.append(("execute", threading.get_ident(), name))
        return gated()

    hooks = BatchExecutionHooks(
        step_flow=step_flow,
        settle_step=settle_step,
        execute_serial=lambda call: agent.execute_tool(
            call.tool_name, call.arguments, call_context=call.context
        ),
        plan_operation=operation,
        dispatch=dispatch,
        activate=active.append,
        acting=lambda call: None,
        uncertain=lambda: setattr(agent.tool_context, "execution_uncertain", True),
    )
    runner = ToolBatchRunner(
        context=agent.tool_context,
        registry=agent.tools,
        allowed_tools=agent._tool_names,
        executor=agent._get_tool_executor(),
        progress=progress,
        session=agent.session,
        hooks=hooks,
        cancel_token=cancel,
        expired=lambda: False,
        tool_timeout_s=0,
        emit=lambda kind, payload: events.append((kind, payload)),
    )
    calls = [ToolCall("read_file", {"path": "value.py", "start": i}, f"read-{i}") for i in (1, 2)]
    return (
        agent,
        runner,
        calls,
        recorder,
        owner,
        log,
        events,
        active,
        steps_closed,
        operations_closed,
    )


def test_services_run_real_parallel_reads_without_loop_and_commit_on_owner(tmp_path):
    agent, runner, calls, _, owner, log, events, active, closed, operations = independent_runtime(
        tmp_path
    )
    barrier = threading.Barrier(2)
    original = agent.tools["read_file"]["run_with_context"]

    def overlapping(context, args):
        barrier.wait(timeout=3)
        return original(context, args)

    agent.tools["read_file"]["run_with_context"] = overlapping
    results, refs = runner.run(calls)
    assert [block["tool_use_id"] for block in results] == ["read-1", "read-2"]
    assert all(thread == owner for kind, thread, _ in log if kind != "execute")
    assert all(thread != owner for kind, thread, _ in log if kind == "execute")
    assert len({thread for kind, thread, _ in log if kind == "execute"}) == 2
    assert closed == ["read-1", "read-2"]
    assert set(operations) == {"read-1", "read-2"}
    observations = agent.session["tool_observations"]
    assert refs == {obs["call_id"]: obs["observation_id"] for obs in observations}
    assert [call.result_ref for call in active[0].calls] == list(refs.values())
    assert all(obs["receipt"]["call_id"] == obs["call_id"] for obs in observations)
    # Execution completion can precede storage; it is not a durable result reference.
    kinds = [kind for kind, _ in events]
    assert kinds.index("tool_call_completed") < kinds.index("observation_stored")
    assert kinds[-1] == "tool_batch_completed"
    assert agent.session["turn_progress"]["calls"][0]["result_ref"] == refs["read-1"]
    assert not hasattr(agent, "_loop")


def test_queued_cancellation_records_every_call_without_dispatch(tmp_path):
    token = CancellationToken()
    token.cancel("user")
    agent, runner, calls, _, _, log, _, active, closed, operations = independent_runtime(
        tmp_path, cancel=token
    )
    results, refs = runner.run(calls)
    assert len(results) == len(refs) == 2
    assert not any(kind == "execute" for kind, _, _ in log)
    assert not operations and closed == ["read-1", "read-2"]
    assert all(obs["status"] == "cancelled" for obs in agent.session["tool_observations"])
    assert all(not call.context.budget_reserved for call in active[0].calls)


@pytest.mark.parametrize("service", ["recorder", "runner"])
def test_worker_cannot_use_owner_services(tmp_path, service):
    agent, runner, calls, recorder, _, log, _, active, _, _ = independent_runtime(tmp_path)
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = (
            worker.submit(
                runner.run,
                calls,
            )
            if service == "runner"
            else worker.submit(
                recorder.record,
                CanonicalToolCall.create("read_file", {"path": "value.py"}),
                ToolResult(content="value = 1"),
                duration_ms=0,
                metadata={},
                source_version="",
                idempotency_key="none",
            )
        )
        with pytest.raises(RuntimeError, match="requires_owner"):
            future.result(timeout=3)
    assert not log and not active
    assert "tool_observations" not in agent.session


def test_storage_failure_closes_handle_and_publishes_no_result_reference(tmp_path, monkeypatch):
    agent, _, _, recorder, _, _, events, _, _, _ = independent_runtime(tmp_path)
    action = {"idempotency_key": "pending", "status": "verified"}
    agent.session["action_ledger"] = [action]
    result = ToolResult(content="value = 1")
    closed = []
    original_close = ObservationStore.close

    def close(store):
        closed.append(store)
        original_close(store)

    def fail_put(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ObservationStore, "put", fail_put)
    monkeypatch.setattr(ObservationStore, "close", close)
    with pytest.raises(OSError, match="disk full"):
        recorder.record(
            CanonicalToolCall.create("read_file", {"path": "value.py"}),
            result,
            duration_ms=0,
            metadata=result.metadata,
            source_version="",
            idempotency_key="pending",
        )
    assert len(closed) == 1
    assert "observation_id" not in result.metadata and "result_ref" not in action
    assert "tool_observations" not in agent.session and not events


def test_settlement_failure_closes_prepared_steps_and_preserves_checkpoint(tmp_path):
    agent, runner, calls, _, _, _, _, active, closed, operations = independent_runtime(
        tmp_path, fail_settle=True
    )
    with pytest.raises(OSError, match="observation storage failed"):
        runner.run(calls)
    assert set(closed) == set(operations) == {"read-1", "read-2"}
    assert not any(call.result_ref for call in active[0].calls)
    checkpoint = agent.session["turn_progress"]
    assert checkpoint["batch_id"] == active[0].batch_id
    assert all(call["receipt"] and not call["result_ref"] for call in checkpoint["calls"])


def test_preparation_failure_closes_already_prepared_operation(tmp_path):
    agent, runner, calls, _, _, _, _, active, closed, operations = independent_runtime(tmp_path)
    original = runner.hooks.step_flow

    def fail_second(call, prepared):
        if call.ordinal == 1:
            raise OSError("owner preparation failed")
        return (yield from original(call, prepared))

    runner.hooks = replace(runner.hooks, step_flow=fail_second)
    with pytest.raises(OSError, match="owner preparation failed"):
        runner.run(calls)
    assert closed == operations == ["read-1"]
    assert active[0].calls[0].context.cancel_token.is_cancelled
    assert not any(call.result_ref for call in active[0].calls)
    assert agent.session["turn_progress"]["batch_id"] == active[0].batch_id


def test_unconfirmed_timeout_marks_runtime_uncertain_and_keeps_all_refs(tmp_path):
    agent, runner, calls, _, _, _, _, active, _, _ = independent_runtime(tmp_path)
    release = threading.Event()
    finished = threading.Barrier(3)

    def uncooperative_read(context, args):
        try:
            release.wait(timeout=3)
            return ToolResult(content="late output")
        finally:
            finished.wait(timeout=3)

    agent.tools["read_file"]["run_with_context"] = uncooperative_read
    runner.tool_timeout_s = 0.01
    try:
        results, refs = runner.run(calls)
        assert agent.tool_context.execution_uncertain
        assert len(results) == len(refs) == 2
        assert all(call.status == "uncertain" for call in active[0].calls)
        assert all("late output" not in block["content"] for block in results)
    finally:
        release.set()
        finished.wait(timeout=3)


def test_mcp_raw_record_and_matching_action_keep_exact_reference(tmp_path):
    agent, _, _, recorder, _, _, _, _, _, _ = independent_runtime(tmp_path)
    first = {"idempotency_key": "other", "result_ref": "other-result"}
    current = {"idempotency_key": "current"}
    agent.session["action_ledger"] = [first, current]
    result = ToolResult(
        content="summary",
        metadata={
            "raw_result": {"rows": [1, 2]},
            "provider": "mcp",
            "mcp_server": "local",
        },
    )
    recorded = recorder.record(
        CanonicalToolCall.create("custom", {}, call_id="mcp-call"),
        result,
        duration_ms=12,
        metadata=result.metadata,
        source_version="v1",
        idempotency_key="current",
    )
    assert recorded.observation["provider"] == "mcp"
    assert recorded.observation["server"] == "local"
    assert (
        current["result_ref"] == result.metadata["observation_id"] == recorded.stored.observation_id
    )
    assert first["result_ref"] == "other-result"
    store = ObservationStore(agent.session, root=str(tmp_path))
    try:
        assert '"rows": [1, 2]' in store.expand(recorded.stored.observation_id)
    finally:
        store.close()
