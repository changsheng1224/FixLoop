"""ModelClient HTTP 429/5xx 重试单测。"""

import io
import json
from unittest.mock import patch

import pytest
import urllib.error

from agent_runtime.providers.clients import AnthropicCompatibleModelClient
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


def _http_error(code: int, *, retry_after: str | None = None):
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        url="http://test/messages",
        code=code,
        msg="err",
        hdrs=headers,
        fp=io.BytesIO(b""),
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
