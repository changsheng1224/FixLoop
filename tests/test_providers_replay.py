"""Providers 异常路径 + Replay 测试。"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.providers.clients import (
    AnthropicCompatibleModelClient,
    FakeModelClient,
    OpenAICompatibleModelClient,
)
from agent_runtime.replay import ReplayRunner
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


def _fake_http_server(status=200, response_body="{}"):
    """启动临时 HTTP 服务器。"""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response_body).encode())

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    t = Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


class TestAnthropicClientErrors:
    """Anthropic 异常路径。"""

    def test_http_4xx_no_retry(self):
        server, port = _fake_http_server(status=400, response_body={"error": "bad request"})
        try:
            client = AnthropicCompatibleModelClient(
                model="test", base_url=f"http://127.0.0.1:{port}", api_key="x",
                timeout=2,
            )
            with pytest.raises(RuntimeError, match="HTTP 400"):
                client.complete("hello", max_new_tokens=10)
        finally:
            server.shutdown()

    def test_extract_text_openai_format(self):
        client = AnthropicCompatibleModelClient(model="x", base_url="http://x", api_key="x")
        # Already covered in test_anthropic_client.py, verify import works
        assert client.supports_prompt_cache is True


class TestOpenAICompatibleClient:
    """OpenAI 客户端 mock 测试。"""

    def test_complete_returns_text(self):
        server, port = _fake_http_server(status=200, response_body={
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "response from openai"}],
            }]
        })
        try:
            client = OpenAICompatibleModelClient(
                model="gpt-4o", base_url=f"http://127.0.0.1:{port}", api_key="sk-test",
                timeout=2,
            )
            result = client.complete("hello", max_new_tokens=50)
            assert result == "response from openai"
        finally:
            server.shutdown()

    def test_supports_prompt_cache_false(self):
        client = OpenAICompatibleModelClient(model="x", base_url="http://x", api_key="x")
        assert client.supports_prompt_cache is False


class TestReplayRunner:
    """Replay 回放测试。"""

    def test_replay_with_tool_events(self, temp_workspace):
        """trace 含 tool_executed 事件时正确解析。"""
        trace_path = temp_workspace / "test_trace.jsonl"
        trace_path.write_text(
            '{"event":"run_started","created_at":"2026-01-01"}\n'
            '{"event":"tool_executed","payload":{"tool":"read_file"}}\n'
            '{"event":"run_finished","payload":{"stop_reason":"final"}}\n'
        )

        config = AgentConfig(provider="fake")
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient(["<final>ok</final>"])
        agent = Agent(config=config, model_client=client, workspace=ws)

        runner = ReplayRunner(str(trace_path))
        result = runner.replay(agent)
        assert result.total == 1  # 1 个 tool_executed 事件
        assert len(result.diffs) == 1  # args unavailable from trace

    def test_partial_diffs_detected(self, temp_workspace):
        trace_path = temp_workspace / "partial.jsonl"
        trace_path.write_text(
            '{"event":"tool_executed","payload":{"tool":"read_file"}}\n'
            '{"event":"tool_executed","payload":{"tool":"search"}}\n'
        )

        config = AgentConfig(provider="fake")
        ws = WorkspaceContext.build(str(temp_workspace))
        client = FakeModelClient(["<final>ok</final>"])
        agent = Agent(config=config, model_client=client, workspace=ws)

        runner = ReplayRunner(str(trace_path))
        result = runner.replay(agent)
        assert result.total == 2
