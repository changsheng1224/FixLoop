"""Selected source evidence must reach the actual model request."""

from __future__ import annotations

import json

from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import ContextManager
from agent_runtime.providers.clients import FakeNativeToolClient
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


def test_selected_source_in_later_native_model_request(tmp_path):
    (tmp_path / "service.py").write_text(
        "def render(value):\n    return f'<{value}>'\n", encoding="utf-8"
    )
    class RecordingClient(FakeNativeToolClient):
        def __init__(self, outputs):
            super().__init__(outputs)
            self.requests = []

        def complete_turn(self, request):
            self.requests.append(request)
            return super().complete_turn(request)

    client = RecordingClient([
        '<tool>{"name":"read_file","args":{"path":"service.py"}}</tool>',
        '<tool>{"name":"code_relations","args":{}}</tool>',
        "<final>done</final>",
    ])
    agent = Agent(
        config=AgentConfig(provider="fake", max_steps=5,
                           code_exploration={"mode": "relations"}),
        model_client=client, workspace=WorkspaceContext.build(str(tmp_path)),
        cwd=str(tmp_path),
    )
    assert agent.ask("Find the render implementation", skip_plan=True) == "done"
    assert len(client.prompts) == 3
    third = client.requests[2]
    request_text = json.dumps(third.messages, ensure_ascii=False)
    assert "## 当前代码片段" in request_text
    assert "def render(value):" in request_text
    assert "[source=OBS-" in request_text
    assert request_text.count("def render(value):") == 1
    manifest = agent.session["context_manifest"]
    assert any(item.startswith("source:") for item in manifest["selected_context_ids"])
    assert agent.tool_context.exploration_service is None


def test_changed_source_is_not_injected_again(tmp_path):
    from tests.test_code_relations import _observe, _setup

    root, state, context, service = _setup(tmp_path, "test_relation")
    _observe(state, context, service, "service.py")
    service.relations({})
    agent = Agent(
        config=AgentConfig(provider="fake", code_exploration={"mode": "relations"}),
        model_client=FakeNativeToolClient(["<final>done</final>"]),
        workspace=WorkspaceContext.build(str(root)), cwd=str(root),
        tool_context=context,
    )
    agent.tool_context.exploration_service = service
    # Use the current Agent session as the task scope for the pending view.
    service.context.observation_state = state
    manager = ContextManager(agent)
    before, _ = manager.build_dynamic_context("inspect render")
    assert "def render" in before
    (root / "service.py").write_text("def changed():\n    pass\n", encoding="utf-8")
    after, _ = manager.build_dynamic_context("inspect render")
    assert "def render" not in after
    assert "def changed" not in after
    assert not service.pending_candidates
    service.close()
