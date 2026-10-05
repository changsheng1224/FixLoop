"""T1-T6: barriers prove overlap, capacity, pairing and bounded cleanup."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_runtime.cancellation import CancellationToken
from agent_runtime.model_turn import ToolCall
from agent_runtime.read_permits import ReadPermitPool
from agent_runtime.tool_batch import ToolBatchProtocolError, ToolBatchScheduler, ToolCallBatch
from agent_runtime.tool_context import ToolContext
from agent_runtime.tool_result import ToolResult
from agent_runtime.tools import build_tool_registry
from agent_runtime.turn_progress import TurnEventEmitter


def batch_for(tmp_path, calls):
    ctx = ToolContext(str(tmp_path))
    return ToolCallBatch.create(
        calls, run_id="run", turn_id="turn", context=ctx, registry=build_tool_registry(ctx)
    )


def scheduler_for(pool=None, **kwargs):
    events = []
    emitter = TurnEventEmitter("run", "turn", lambda kind, event: events.append(event))
    return ToolBatchScheduler(pool or ReadPermitPool(), emitter, **kwargs), events


def test_same_name_calls_overlap_and_keep_order(tmp_path):
    batch = batch_for(
        tmp_path, [ToolCall("read_file", {"path": str(i)}, f"c{i}") for i in range(4)]
    )
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = peak = 0

    def prepare(call):
        def work():
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=3)
            with lock:
                active -= 1
            return ToolResult(content=call.arguments["path"])

        return work

    scheduler, events = scheduler_for()
    results = scheduler.run(batch, prepare, lambda call, result: result)
    assert peak == 2
    assert [result.content for result in results] == ["0", "1", "2", "3"]
    assert [result.receipt["call_id"] for result in results] == ["c0", "c1", "c2", "c3"]
    assert len({result.receipt["args_hash"] for result in results}) == 4
    assert len({call.context.idempotency_key for call in batch.calls}) == 4
    assert any(event.get("reason") == "waiting_capacity" for event in events)


@pytest.mark.parametrize(
    "calls",
    [
        [ToolCall("read_file", {}, "")],
        [ToolCall("read_file", {}, "x"), ToolCall("list_files", {}, "x")],
        [ToolCall("missing", {}, "x")],
        [ToolCall("read_file", [], "x")],
        [ToolCall("read_file", {"path": object()}, "x")],
        [],
        None,
    ],
)
def test_protocol_rejection_before_dispatch(tmp_path, calls):
    with pytest.raises(ToolBatchProtocolError):
        batch_for(tmp_path, calls)


@pytest.mark.parametrize("side_effect_tool", ["write_file", "run_shell", "quick_test"])
def test_mixed_batch_serial_original_order(tmp_path, side_effect_tool):
    batch = batch_for(
        tmp_path,
        [
            ToolCall("read_file", {}, "r"),
            ToolCall(side_effect_tool, {}, "w"),
            ToolCall("read_file", {}, "r2"),
        ],
    )
    assert not batch.parallel
    executed = []
    scheduler, _ = scheduler_for()
    scheduler.run(
        batch,
        lambda call: lambda: executed.append(call.call_id) or ToolResult(content="ok"),
        lambda call, result: result,
    )
    assert executed == ["r", "w", "r2"]


def test_more_than_four_calls_degrade_without_dropping(tmp_path):
    batch = batch_for(tmp_path, [ToolCall("read_file", {}, str(i)) for i in range(5)])
    assert not batch.parallel and batch.downgrade_reason == "capacity"
    scheduler, _ = scheduler_for()
    assert (
        len(
            scheduler.run(
                batch, lambda call: lambda: ToolResult(content="ok"), lambda call, result: result
            )
        )
        == 5
    )


def test_failed_read_does_not_block_sibling(tmp_path):
    batch = batch_for(
        tmp_path, [ToolCall("read_file", {}, "bad"), ToolCall("list_files", {}, "ok")]
    )

    def prepare(call):
        def work():
            if call.call_id == "bad":
                raise RuntimeError("private content must not leak")
            return ToolResult(content="ok")

        return work

    scheduler, events = scheduler_for()
    results = scheduler.run(batch, prepare, lambda call, result: result)
    assert [result.status for result in results] == ["error", "success"]
    assert "private content" not in str(events)


def test_cancel_running_and_queued_and_discard_late_result(tmp_path):
    batch = batch_for(tmp_path, [ToolCall("read_file", {}, str(i)) for i in range(4)])
    token = CancellationToken()
    barrier = threading.Barrier(3)
    release = threading.Event()
    pool = ReadPermitPool()
    scheduler, events = scheduler_for(pool, cleanup_s=0.02)
    executed = []

    def prepare(call):
        def work():
            executed.append(call.call_id)
            barrier.wait(timeout=3)
            release.wait(timeout=3)
            return ToolResult(content="late result")

        return work

    with ThreadPoolExecutor(max_workers=1) as owner:
        future = owner.submit(
            scheduler.run, batch, prepare, lambda call, result: result, cancel_token=token
        )
        try:
            barrier.wait(timeout=3)
            token.cancel()
            results = future.result(timeout=3)
            assert [result.status for result in results] == [
                "uncertain",
                "uncertain",
                "cancelled",
                "cancelled",
            ]
            assert sorted(executed) == ["0", "1"]
            assert not pool.acquire()  # live abandoned readers still occupy capacity
            saved_events = list(events)
        finally:
            release.set()
    assert events == saved_events
    assert all(result.content != "late result" for result in results)


def test_cooperative_cancel_confirms_worker_return(tmp_path):
    batch = batch_for(tmp_path, [ToolCall("read_file", {}, str(i)) for i in range(3)])
    token = CancellationToken()
    barrier = threading.Barrier(3)

    def prepare(call):
        def work():
            barrier.wait(timeout=3)
            while not call.context.cancel_token.is_cancelled:
                threading.Event().wait(0.005)
            return ToolResult(content="stopped")

        return work

    scheduler, _ = scheduler_for()
    with ThreadPoolExecutor(max_workers=1) as owner:
        future = owner.submit(
            scheduler.run, batch, prepare, lambda call, result: result, cancel_token=token
        )
        barrier.wait(timeout=3)
        token.cancel()
        assert [result.status for result in future.result(timeout=3)] == ["cancelled"] * 3


def test_timeout_keeps_abandoned_capacity_and_cancels_undispatchable_calls(tmp_path):
    batch = batch_for(tmp_path, [ToolCall("read_file", {}, str(i)) for i in range(4)])
    for call in batch.calls:
        call.context.timeout_s = 0.02
    release = threading.Event()
    scheduler, events = scheduler_for(cleanup_s=0.02)
    try:
        results = scheduler.run(
            batch,
            lambda call: lambda: release.wait(timeout=3) or "late",
            lambda call, result: result,
        )
        assert [result.status for result in results] == [
            "uncertain",
            "uncertain",
            "cancelled",
            "cancelled",
        ]
        assert [result.error_code for result in results[:2]] == ["tool_timeout"] * 2
        assert sum(e["event"] == "tool_call_started" for e in events) == 2
    finally:
        release.set()


def test_batch_freezes_registry_and_default_path_resolver(tmp_path):
    context = ToolContext(str(tmp_path))
    registry = build_tool_registry(context)
    handler = registry["read_file"]["run_with_context"]
    batch = ToolCallBatch.create(
        [ToolCall("read_file", {}, "x")],
        run_id="r",
        turn_id="t",
        context=context,
        registry=registry,
    )
    registry["read_file"]["run_with_context"] = lambda *_: "unexpected handler"
    context.root = str(tmp_path.parent)
    isolated = batch.calls[0].context
    assert isolated.registry["read_file"]["run_with_context"] is handler
    assert isolated.tool_context.resolve("file.txt") == tmp_path / "file.txt"


def test_cancel_during_future_collection_never_reuses_returned_success(tmp_path):
    batch = batch_for(tmp_path, [ToolCall("read_file", {}, "a")])
    token = CancellationToken()
    scheduler, _ = scheduler_for()

    def prepare(call):
        def work():
            token.cancel()
            return ToolResult(content="completed after cancellation")

        return work

    results = scheduler.run(batch, prepare, lambda call, result: result, cancel_token=token)
    assert results[0].status == "cancelled"
    assert results[0].content != "completed after cancellation"
