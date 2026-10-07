"""ModelClient HTTP 429/5xx 重试单测。"""

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from agent_runtime.providers.clients import AnthropicCompatibleModelClient
from agent_runtime.providers.contracts import ProviderError, ProviderErrorCode
from agent_runtime.providers.retry_policy import RateLimitExceededError


def _ok_response():
    body = json.dumps(
        {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    ).encode("utf-8")

    class FakeResp:
        def __init__(self):
            self._sent = False

        def read(self, size=-1):
            if self._sent:
                return b""
            self._sent = True
            return body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    return FakeResp()


def _http_error(
    code: int,
    *,
    retry_after: str | None = None,
    body: bytes = b"",
    extra_headers: dict[str, str] | None = None,
):
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    headers.update(extra_headers or {})
    return urllib.error.HTTPError(
        url="http://test/messages",
        code=code,
        msg="err",
        hdrs=headers,
        fp=io.BytesIO(body),
    )


class TestAnthropicPostMessagesRetry:
    def _client(self):
        return AnthropicCompatibleModelClient(
            model="test",
            base_url="http://test",
            api_key="k",
        )

    @patch("agent_runtime.providers.clients.time.sleep")
    @patch("urllib.request.urlopen")
    def test_retries_429_then_succeeds(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = [
            _http_error(429, retry_after="10"),
            _ok_response(),
        ]
        client = self._client()
        data, _ = client._post_messages(b"{}")
        assert data["content"][0]["text"] == "ok"
        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once()
        assert 5.0 <= mock_sleep.call_args[0][0] <= 10.0

    @patch("agent_runtime.providers.clients.time.sleep")
    @patch("urllib.request.urlopen")
    def test_429_exhausted_raises_rate_limit_error(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = [_http_error(429, retry_after="1")] * 3
        client = self._client()
        with pytest.raises(RateLimitExceededError):
            client._post_messages(b"{}")
        assert mock_urlopen.call_count == 3

    @patch("agent_runtime.providers.clients.time.sleep")
    @patch("urllib.request.urlopen")
    def test_4xx_non_429_not_retried(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = [_http_error(401)]
        client = self._client()
        with pytest.raises(RuntimeError, match="HTTP 401"):
            client._post_messages(b"{}")
        assert mock_urlopen.call_count == 1
        mock_sleep.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_400_captures_sanitized_protocol_diagnostics(self, mock_urlopen):
        response_body = json.dumps(
            {
                "error": {
                    "type": "invalid_request_error",
                    "message": "tool_use has no tool_result; key=sk-supersecret123",
                },
                "request_id": "req-123",
            }
        ).encode("utf-8")
        mock_urlopen.side_effect = _http_error(
            400,
            body=response_body,
            extra_headers={"Content-Type": "application/json"},
        )
        request_body = json.dumps(
            {
                "model": "test",
                "system": "TOP SECRET SOURCE",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call-1",
                                "name": "read_file",
                                "input": {"path": "secret.py"},
                            }
                        ],
                    }
                ],
                "tools": [{"name": "read_file"}],
                "max_tokens": 4096,
            }
        ).encode("utf-8")

        with pytest.raises(ProviderError) as caught:
            self._client()._post_messages(request_body)

        error = caught.value
        assert error.code == ProviderErrorCode.PROTOCOL
        request = error.metadata["request"]
        response = error.metadata["response"]
        assert request["message_count"] == 1
        assert request["tool_use_count"] == 1
        assert request["tool_result_count"] == 0
        assert request["unmatched_tool_use_count"] == 1
        assert request["tool_schema_count"] == 1
        assert response["error_type"] == "invalid_request_error"
        assert response["request_id"] == "req-123"
        serialized = json.dumps(error.to_trace_payload())
        assert "TOP SECRET SOURCE" not in serialized
        assert "secret.py" not in serialized
        assert "sk-supersecret123" not in serialized
        assert "<redacted>" in serialized

    @patch("agent_runtime.providers.clients.time.sleep")
    @patch("urllib.request.urlopen")
    def test_5xx_retries_with_jitter(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = [
            _http_error(503),
            _ok_response(),
        ]
        client = self._client()
        client._post_messages(b"{}")
        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once()
        assert mock_sleep.call_args[0][0] <= 2.0


def test_complete_turn_serializes_required_tool_choice(monkeypatch):
    from agent_runtime.model_timing import ModelCallTiming
    from agent_runtime.model_turn import ModelTurnRequest, ToolChoice, ToolChoiceMode

    client = AnthropicCompatibleModelClient(model="test", base_url="http://test", api_key="k")
    captured = {}

    def fake_post(body, *, deadline=None):
        captured.update(json.loads(body.decode("utf-8")))
        return (
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "apply_patch",
                        "input": {"patch": "*** Begin Patch\n*** End Patch"},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            ModelCallTiming(ttft_ms=0, total_ms=0, output_tokens=1),
        )

    monkeypatch.setattr(client, "_post_messages", fake_post)
    client.complete_turn(
        ModelTurnRequest(
            system_prompt="repair",
            messages=[{"role": "user", "content": "fix"}],
            tools=[
                {
                    "name": "apply_patch",
                    "description": "patch",
                    "input_schema": {"type": "object"},
                }
            ],
            tool_choice=ToolChoice(ToolChoiceMode.REQUIRED),
        )
    )

    assert captured["tool_choice"] == {"type": "any"}


def test_complete_turn_omits_tool_choice_by_default(monkeypatch):
    from agent_runtime.model_timing import ModelCallTiming
    from agent_runtime.model_turn import ModelTurnRequest

    client = AnthropicCompatibleModelClient(model="test", base_url="http://test", api_key="k")
    captured = {}

    def fake_post(body, *, deadline=None):
        captured.update(json.loads(body.decode("utf-8")))
        return (
            {
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            ModelCallTiming(ttft_ms=0, total_ms=0, output_tokens=1),
        )

    monkeypatch.setattr(client, "_post_messages", fake_post)
    client.complete_turn(
        ModelTurnRequest(
            system_prompt="repair",
            messages=[{"role": "user", "content": "fix"}],
            tools=[],
        )
    )

    assert "tool_choice" not in captured
