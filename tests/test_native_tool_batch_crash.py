"""T8: real process exit, journal facts and display replay on native paths."""

import json
import os
import subprocess
import sys

import pytest

from agent_runtime.plan_runtime.recovery import recover
from agent_runtime.turn_progress import restore_progress
from tests.plan_support import session_for


@pytest.mark.parametrize(
    "kind,cut", [("read", "tool_result_recorded"), ("write", "tool_dispatched")]
)
def test_native_process_exit_uses_plan_receipts_not_ui_for_recovery(tmp_path, kind, cut):
    (tmp_path / "value.py").write_text("value = 1\nother = 2\n")
    code = """
import os, sys
from agent_runtime.agent_loop import AgentLoop
from agent_runtime.model_turn import ToolCall
from agent_runtime.workspace import WorkspaceContext
from tests.plan_support import session_for, simple_plan, through_analysis
from tests.test_native_tool_batch import make_agent
root, kind, cut = sys.argv[1:]
with session_for(root) as session:
    session.create(simple_plan(session))
    session.configure_long_task('inspect')
    if kind == 'write':
        through_analysis(session)
        calls = [ToolCall('write_file', {'path': 'value.py', 'content': 'value = 3\\n'}, 'write-id'),
                 ToolCall('read_file', {'path': 'value.py'}, 'read-after')]
        node_id = 'edit'
    else:
        calls = [ToolCall('read_file', {'path': 'value.py', 'start': i}, 'read-'+str(i)) for i in (1,2)]
        node_id = 'read-0'
    agent, _ = make_agent(WorkspaceContext.build(root), calls)
    # Exercise the crash cut with a complete task and room for the native schema.
    agent.config.prompt_budget = 6000
    agent.config.hard_cap = 6000
    agent._plan_session = session
    agent.shared_run_id = session.identity['run_id']
    session.fault = lambda point: os._exit(73) if point == cut else None
    session.run_node(node_id, lambda attempt: (
        AgentLoop(agent).run('inspect', skip_plan=True), session.tool_result(attempt))[1])
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), kind, cut],
        timeout=30,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.getcwd()},
    )
    assert child.returncode == 73, child.stderr
    with session_for(tmp_path) as restored:
        operations = [
            operation
            for operation in restored.store.latest("operation", "operation_id").values()
            if operation.get("batch_id")
        ]
        assert operations and all(operation["call_id"] for operation in operations)
        trace = tmp_path / ".agent" / "runs" / "run" / "trace.jsonl"
        events = [
            json.loads(line)["payload"]
            for line in trace.read_text(encoding="utf-8").splitlines()
            if "event_seq" in json.loads(line).get("payload", {})
        ]
        checkpoint = {
            "run_id": "run",
            "turn_id": operations[0]["turn_id"],
            "batch_id": operations[0]["batch_id"],
            "events": events,
            "event_seq": max(event["event_seq"] for event in events),
        }
        # Deliberately remove a trace event: committed receipts still reconstruct
        # the confirmed display, while replay incompleteness stays visible.
        checkpoint["events"] = events[1:]
        display = restore_progress(checkpoint, operations=operations)
        assert display["progress_replay_incomplete"]
        calls = display["turns"][checkpoint["turn_id"]]["calls"]
        confirmed = [operation for operation in operations if operation.get("receipt")]
        assert {
            call["call_id"] for call in calls if call.get("confirmation") == "plan_receipt"
        } == {operation["call_id"] for operation in confirmed}
        report = recover(restored)
        if kind == "read":
            assert report["restarted_read"] == ["read-0"]
            assert restored.plan.node("read-0").status == "ready"
            assert any(call.get("confirmation") == "plan_receipt" for call in calls)
        else:
            assert restored.plan.node("edit").status == "uncertain"
            assert report["uncertain"]
            assert not confirmed
            assert (tmp_path / "value.py").read_text() == "value = 1\nother = 2\n"
            with pytest.raises(ValueError, match="not_ready"):
                restored.prepare("edit")
