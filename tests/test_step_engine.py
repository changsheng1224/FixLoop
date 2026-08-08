"""Shared StepEngine lifecycle tests."""

from agent_runtime.runtime_contracts import RuntimePhase
from agent_runtime.step_engine import StepEngine
from agent_runtime.task_state import TaskState


def test_step_engine_projects_protocol_phases_into_runtime_contract():
    state = TaskState.create(user_request="inspect")
    events = []
    engine = StepEngine(state, lambda name, payload: events.append((name, payload)))

    engine.enter("reasoning", step=1, path="native")
    engine.enter("acting", step=1, path="native", tool="read_file")

    assert state.phase == "acting"
    assert state.runtime_contract["phase"] == RuntimePhase.ACTING.value
    assert state.runtime_contract["revision"] == 2
    assert events == []
