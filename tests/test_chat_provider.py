"""Wire-contract tests for L1 ordinary chat streaming, using local HTTP fixtures."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_runtime.cancellation import CancellationToken, CancelledError
from agent_runtime.model_turn import FinishKind, ModelTurnRequest
from agent_runtime.providers.chat import stream_chat_turn
from agent_runtime.providers.clients import (
    AnthropicCompatibleModelClient,
    FakeModelClient,
    OllamaModelClient,
    OpenAICompatibleModelClient,
)
from agent_runtime.providers.contracts import ProviderError, ProviderErrorCode


@contextmanager
def provider_server(events, *, ndjson=False, status=200):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):  # noqa: N802
            requests.append(
                (
                    self.path,
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode()),
                )
            )
            self.send_response(status)
            self.send_header(
                "Content-Type", "application/x-ndjson" if ndjson else "text/event-stream"
            )
            self.end_headers()
            for event in events:
                encoded = json.dumps(event, ensure_ascii=False).encode("utf-8")
                self.wfile.write(encoded + b"\n" if ndjson else b"data: " + encoded + b"\n\n")
                self.wfile.flush()

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            thread.join(3)


MESSAGES = [
    {"role": "user", "content": "先说你好"},
    {"role": "assistant", "content": "你好"},
    {"role": "user", "content": "再说世界"},
]


def call(client, *, token=None, callback=None):
    chunks = []
    request = ModelTurnRequest("help with writing", MESSAGES, max_output_tokens=512)
    response = stream_chat_turn(
        client,
        request,
        cancel_token=token or CancellationToken(),
        on_chunk=callback or chunks.append,
    )
    return response, chunks


def test_anthropic_stream_preserves_roles_ignores_reasoning_and_records_usage():
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 20}}},
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "private"}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "世界"}},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 2},
        },
        {"type": "message_stop"},
    ]
    with provider_server(events) as (url, requests):
        response, chunks = call(AnthropicCompatibleModelClient("test", url, "test"))
    assert response.text == "世界" and chunks == ["世界"]
    assert response.finish.kind == FinishKind.TEXT_COMPLETE
    assert response.usage["input_tokens"] == 20 and response.usage["output_tokens"] == 2
    path, body = requests[0]
    assert path == "/messages" and body["messages"] == MESSAGES
    assert body["stream"] is True and "tools" not in body


def test_openai_stream_does_not_duplicate_final_output_and_preserves_roles():
    events = [
        {"type": "response.output_text.delta", "delta": "世"},
        {"type": "response.output_text.delta", "delta": "界"},
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 12, "output_tokens": 2},
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "世界"}]}
                ],
            },
        },
    ]
    with provider_server(events) as (url, requests):
        response, chunks = call(OpenAICompatibleModelClient(base_url=url))
    assert chunks == ["世", "界"] and response.text == "世界"
    assert requests[0][0] == "/responses" and requests[0][1]["input"] == MESSAGES
    assert response.usage["input_tokens"] == 12


def test_openai_completed_tool_call_is_rejected_without_item_events():
    events = [
        {"type": "response.output_text.delta", "delta": "partial"},
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [{"type": "function_call", "name": "write_file"}],
            },
        },
    ]
    chunks = []
    with provider_server(events) as (url, _), pytest.raises(ProviderError) as error:
        call(OpenAICompatibleModelClient(base_url=url), callback=chunks.append)
    assert error.value.code == ProviderErrorCode.PROTOCOL
    assert chunks == ["partial"]


@pytest.mark.parametrize("reason", [None, "future_reason", "content_filter"])
def test_openai_incomplete_event_cannot_become_text_complete(reason):
    events = [
        {"type": "response.output_text.delta", "delta": "partial"},
        {
            "type": "response.incomplete",
            "response": {"status": "incomplete", "incomplete_details": {"reason": reason}},
        },
    ]
    with provider_server(events) as (url, _):
        response, chunks = call(OpenAICompatibleModelClient(base_url=url))
    assert chunks == ["partial"]
    assert response.finish.kind != FinishKind.TEXT_COMPLETE


def test_ollama_uses_chat_endpoint_and_records_usage():
    events = [
        {"message": {"role": "assistant", "content": "世界"}, "done": False},
        {
            "message": {"content": ""},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 9,
            "eval_count": 2,
        },
    ]
    with provider_server(events, ndjson=True) as (url, requests):
        response, chunks = call(OllamaModelClient(host=url))
    assert response.text == "世界" and chunks == ["世界"]
    assert requests[0][0] == "/api/chat"
    assert requests[0][1]["messages"][1:] == MESSAGES
    assert response.usage["output_tokens"] == 2


@pytest.mark.parametrize(
    "reason,finish",
    [("max_tokens", FinishKind.MAX_OUTPUT_TOKENS), ("content_filter", FinishKind.CONTENT_FILTER)],
)
def test_anthropic_finish_is_not_assumed_success(reason, finish):
    events = [
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}},
        {"type": "message_delta", "delta": {"stop_reason": reason}},
        {"type": "message_stop"},
    ]
    with provider_server(events) as (url, _):
        response, _ = call(AnthropicCompatibleModelClient("test", url, "test"))
    assert response.finish.kind == finish


@pytest.mark.parametrize(
    "events",
    [
        [{"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}}],
        [{"type": "error", "error": {"message": "secret"}}],
        [
            {
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "name": "write_file"},
            },
            {"type": "message_stop"},
        ],
    ],
)
def test_protocol_failure_or_unexpected_tool_call_is_rejected(events):
    with provider_server(events) as (url, _), pytest.raises(ProviderError):
        call(AnthropicCompatibleModelClient("test", url, "test"))


def test_provider_authentication_failure_is_normalized_without_credentials():
    with provider_server([], status=401) as (url, _), pytest.raises(ProviderError) as error:
        call(AnthropicCompatibleModelClient("test", url, "SECRET"))
    assert error.value.code == ProviderErrorCode.AUTHENTICATION
    assert "SECRET" not in str(error.value)


def test_stream_cancellation_and_fake_plain_text():
    token = CancellationToken()
    token.cancel("user")
    client = FakeModelClient(["never"])
    with pytest.raises(CancelledError):
        call(client, token=token)
    assert client.prompts == []
    response, chunks = call(FakeModelClient(["<final>你好</final>"]))
    assert response.text == "你好" and chunks == ["你好"]
