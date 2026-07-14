"""AgentLoop finalization extraction tests."""

from agent_runtime.agent_loop import AgentLoop
from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.task_state import TaskState


def test_agent_loop_delegates_finalize_to_loop_finalizer(monkeypatch, workspace):
    import agent_runtime.loop_finalizer as loop_finalizer

    agent = Agent(
        config=AgentConfig(provider="fake"),
        model_client=FakeModelClient(["<final>ok</final>"]),
        workspace=workspace,
    )
    loop = AgentLoop(agent)
    ts = TaskState.create(user_request="hello")
    calls = []

    monkeypatch.setattr(
        loop_finalizer,
        "finalize_agent_run",
        lambda actual_loop, actual_ts: calls.append((actual_loop, actual_ts)),
    )

    loop._finalize_run(ts)

    assert calls == [(loop, ts)]
