from __future__ import annotations


def test_terminal_guard_first_decision_wins():
    from agent_runtime.repair_run import RunTerminalGuard

    guard = RunTerminalGuard()
    assert guard.try_finish("r1", "cancelled")
    assert not guard.try_finish("r1", "completed")
    assert guard.event.terminal is True


def test_failure_attribution_is_evidence_backed():
    from agent_runtime.repair_run import attribute_failure

    result = attribute_failure(
        stop_reason="step_limit",
        observations=[
            {"call_id": "obs1", "failure_class": "invalid_args"},
        ],
    )
    assert result["primary"] == "tool_invalid_args"
    assert result["evidence"][0]["observation_id"] == "obs1"


def test_checkpoint_contains_control_state(tmp_path):
    from types import SimpleNamespace

    from agent_runtime.checkpoint import create_checkpoint
    from agent_runtime.task_state import TaskState

    agent = SimpleNamespace(
        _cwd=str(tmp_path),
        config=SimpleNamespace(
            provider="fake", model="m", approval="auto", max_steps=3
        ),
        _prefix=SimpleNamespace(tool_signature="tools", assets_fingerprint=""),
        session={
            "memory": {
                "working": {
                    "recent_files": [],
                    "repair_context": {"changed_files": ["a.py"]},
                    "evidence_ledger": [{"id": "E1"}],
                }
            },
            "_last_tool_observation": {"status": "success"},
        },
    )
    state = TaskState.create(user_request="fix")
    state.phase = "verification"
    state.turn = 2
    checkpoint = create_checkpoint(agent, state, "fix")
    assert checkpoint["phase"] == "verification"
    assert checkpoint["turn"] == 2
    assert checkpoint["evidence_ledger"][0]["id"] == "E1"
    assert checkpoint["last_tool_observation"]["status"] == "success"
