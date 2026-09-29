"""P3 task-local state never becomes checkpoint-restored source context."""

from __future__ import annotations

import json

from agent_runtime.agent_loop import AgentLoop
from agent_runtime.checkpoint import evaluate_resume_state
from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.session_store import SessionStore
from agent_runtime.task_state import TaskState
from agent_runtime.workspace import WorkspaceContext


def _agent(root, outputs):
    return Agent(
        config=AgentConfig(provider="fake", max_steps=5,
                           code_exploration={"mode": "relations"}),
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(str(root)), cwd=str(root),
    )


def test_step_resume_uses_new_epoch_and_no_old_source_context(tmp_path):
    (tmp_path / "service.py").write_text("def render():\n    return 1\n", encoding="utf-8")
    first = _agent(tmp_path, [])
    loop = AgentLoop(first)
    state = TaskState.create(user_request="inspect render")
    state.advance_runtime("reasoning")
    loop._task_state = state
    loop._run_tool_step(
        state, "read_file", {"path": "service.py"}, step=1, path="xml"
    )
    service = first.tool_context.exploration_service
    assert service is not None and service.evidence
    old_epoch = service.epoch
    service.relations({})
    assert service.pending_candidates
    json.dumps(first.session)  # Serializable session contains no service/view object.

    store = SessionStore(str(tmp_path))
    restored = Agent.from_session(
        FakeModelClient(["<final>resumed</final>"]),
        WorkspaceContext.build(str(tmp_path)), store, first.session["id"],
        config=AgentConfig(provider="fake", max_steps=5,
                           code_exploration={"mode": "relations"}),
        cwd=str(tmp_path),
    )
    assert restored is not None
    assert restored.tool_context.exploration_service is None
    restored.session["resume_state"] = evaluate_resume_state(restored)
    assert restored.session["resume_state"]["status"] == "step-resumable"
    assert "resumed" in restored.ask("ignored while step-resuming")
    assert restored.session["exploration_epoch"] != old_epoch
    assert "## 当前代码片段" not in restored.model_client.prompts[0]
    assert restored.tool_context.exploration_service is None
    service.close()
