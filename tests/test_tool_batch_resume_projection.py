"""T8: display references survive truncation and never authorize execution."""

import io
from copy import deepcopy

from agent_runtime.checkpoint import create_checkpoint, evaluate_resume_state
from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.task_state import TaskState
from agent_runtime.turn_progress import TurnEventEmitter, replay_progress


def test_checkpoint_keeps_active_receipts_and_incomplete_ui_is_not_resume_authority(workspace):
    agent = Agent(AgentConfig(provider="fake"), FakeModelClient(["<final>ok</final>"]), workspace)
    ts = TaskState(task_id="task", user_request="inspect", run_id="run")
    events = TurnEventEmitter("run", "turn", lambda *_: None)
    events.emit("turn_started", status="running")
    events.emit(
        "tool_call_completed", batch_id="batch", call_id="confirmed", status="succeeded", ordinal=0
    )
    events.emit(
        "tool_call_started", batch_id="batch", call_id="unknown", status="running", ordinal=1
    )
    progress = events.checkpoint()
    progress["calls"] = [
        {"call_id": "confirmed", "receipt": {"receipt_id": "receipt-confirmed"}},
        {"call_id": "unknown", "receipt": {}},
    ]
    agent.session["turn_progress"] = progress
    agent.session["action_ledger"] = [{"idempotency_key": f"other-{i}"} for i in range(110)]
    cp = create_checkpoint(agent, ts, "inspect", trigger="ask_end")
    assert len(cp["action_ledger"]) == 100
    assert cp["turn_progress"]["calls"][0]["receipt"]["receipt_id"] == "receipt-confirmed"
    baseline = evaluate_resume_state(agent)
    # This display projection is intentionally not the execution journal.
    damaged = deepcopy(progress)
    damaged["events"] = damaged["events"][1:]
    replay = replay_progress(damaged["events"], expected_seq=damaged["event_seq"])
    assert replay["progress_replay_incomplete"]
    cp["turn_progress"] = damaged
    assert evaluate_resume_state(agent)["status"] == baseline["status"]
    assert cp["turn_progress"]["calls"][1]["receipt"] == {}


def test_actual_step_resume_shows_incomplete_progress_without_reexecuting_unknown_call(workspace):
    from agent_runtime.agent_loop import AgentLoop
    from agent_runtime.callbacks import CLIProgressCallback

    agent = Agent(AgentConfig(provider="fake"), FakeModelClient(["<final>ok</final>"]), workspace)
    ts = TaskState(task_id="task", user_request="inspect", run_id="run")
    agent.session["turn_progress"] = {
        "run_id": "run",
        "turn_id": "turn",
        "batch_id": "batch",
        "event_seq": 2,
        "events": [
            {
                "event": "tool_call_started",
                "event_seq": 2,
                "turn_id": "turn",
                "batch_id": "batch",
                "call_id": "unknown",
                "status": "running",
            }
        ],
        "calls": [{"call_id": "unknown", "receipt": {}}],
    }
    cp = create_checkpoint(agent, ts, "inspect", trigger="ask_end")
    output = io.StringIO()
    cli = CLIProgressCallback(output)
    assert "ok" in AgentLoop(agent)._run_from_step_resume({"last_checkpoint": cp}, callback=cli)
    assert "unconfirmed=1" in output.getvalue()
    assert "progress_replay_incomplete=true" in output.getvalue()
    assert not agent.session.get("tool_observations")
    assert agent.quota.quota_summary()["total"]["used"] == 0
