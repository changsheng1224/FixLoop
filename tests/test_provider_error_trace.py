"""Provider HTTP diagnostics are emitted to Trace without request content."""

from unittest.mock import MagicMock

from agent_runtime.agent_loop import AgentLoop
from agent_runtime.canonical_trace import STATUS_ERROR, infer_status
from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.providers.contracts import ProviderError, ProviderErrorCode
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


def test_agent_emits_structured_provider_error(monkeypatch, tmp_path):
    agent = Agent(
        config=AgentConfig(provider="fake"),
        model_client=FakeModelClient(["ok"]),
        workspace=WorkspaceContext.build(str(tmp_path)),
    )
    loop = AgentLoop(agent)
    emitted = []
    monkeypatch.setattr(loop, "_emit", lambda event, payload: emitted.append((event, payload)))
    monkeypatch.setattr(loop, "_complete_run", lambda *_args, **_kwargs: "done")
    state = MagicMock()
    error = ProviderError(
        ProviderErrorCode.PROTOCOL,
        "API request failed (HTTP 400)",
        provider="anthropic-compatible",
        metadata={
            "http_status": 400,
            "request": {"unmatched_tool_use_count": 1},
            "response": {"request_id": "req-123"},
        },
    )

    assert loop._stop_for_api_error(state, error) == "done"
    assert emitted == [
        (
            "provider_error",
            {
                "error_code": "protocol_error",
                "message": "API request failed (HTTP 400)",
                "provider": "anthropic-compatible",
                "retryable": False,
                "retry_after_s": None,
                "exception_type": "",
                "diagnostics": {
                    "http_status": 400,
                    "request": {"unmatched_tool_use_count": 1},
                    "response": {"request_id": "req-123"},
                },
            },
        )
    ]
    state.stop_with_reason.assert_called_once()
    assert infer_status("provider_error", emitted[0][1]) == STATUS_ERROR
