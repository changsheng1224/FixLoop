"""T7/T8: ordered safe callbacks and replay have identical display projections."""

import io

from agent_runtime.callbacks import CallbackChain, CLIProgressCallback
from agent_runtime.turn_progress import TurnEventEmitter, replay_progress, restore_progress


def test_live_progress_matches_reordered_duplicate_replay():
    trace = []
    output = io.StringIO()
    cli = CLIProgressCallback(output)

    def append(kind, event):
        trace.append(event)

    def callback(event):
        assert trace[-1] == event
        cli.on_turn_progress(event)

    emitter = TurnEventEmitter("r", "t", append, CallbackChain([cli]).on_turn_progress)
    emitter.callback = callback
    emitter.emit("turn_started", status="running")
    emitter.emit("tool_batch_created", batch_id="b", status="pending")
    emitter.emit(
        "tool_call_queued",
        batch_id="b",
        call_id="a",
        status="queued",
        reason="waiting_capacity",
        tool_name="read_file",
        ordinal=0,
    )
    emitter.emit("tool_call_started", batch_id="b", call_id="a", status="running")
    emitter.emit("tool_call_queued", batch_id="b", call_id="b", status="queued")
    emitter.emit("tool_call_completed", batch_id="b", call_id="a", status="succeeded")
    emitter.emit("tool_call_completed", batch_id="b", call_id="b", status="failed")
    emitter.phase = "recording"
    emitter.emit("turn_completed", status="completed")
    assert "running=1" in output.getvalue() and "queued=1" in output.getvalue()
    assert replay_progress(list(reversed(trace)) + trace) == cli.progress.snapshot()
    assert not replay_progress(trace)["progress_replay_incomplete"]


def test_missing_or_corrupt_trace_only_marks_display_incomplete():
    assert replay_progress([])["progress_replay_incomplete"]
    assert replay_progress([None, {"turn_id": "t", "event_seq": "bad"}])[
        "progress_replay_incomplete"
    ]
    event = {
        "event": "tool_call_started",
        "turn_id": "t",
        "event_seq": 2,
        "batch_id": "b",
        "call_id": "a",
        "status": "running",
    }
    replay = replay_progress([event], expected_seq=3)
    assert replay["progress_replay_incomplete"]
    assert replay["turns"]["t"]["calls"][0]["status"] == "running"


def test_checkpoint_receipt_overrides_missing_completion_event_and_corrupt_ui_is_tolerated():
    receipt = {"call_id": "a", "run_id": "r", "status": "success", "receipt_id": "receipt-a"}
    checkpoint = {
        "run_id": "r",
        "turn_id": "t",
        "batch_id": "b",
        "event_seq": 3,
        "events": [
            {
                "event": "tool_call_started",
                "turn_id": "t",
                "batch_id": "b",
                "call_id": "a",
                "event_seq": 2,
                "status": "running",
            }
        ],
        "calls": [{"call_id": "a", "receipt": receipt}],
    }
    display = restore_progress(checkpoint)
    assert display["progress_replay_incomplete"]
    call = display["turns"]["t"]["calls"][0]
    assert call["status"] == "succeeded" and call["confirmation"] == "receipt"
    assert restore_progress(["corrupt"])["progress_replay_incomplete"]
    checkpoint["calls"] = [None]
    assert restore_progress(checkpoint)["progress_replay_incomplete"]
