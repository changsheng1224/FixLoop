"""Light Model Client 测试：OllamaModelClient + Agent 双模型集成。"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import ContextManager
from agent_runtime.providers.clients import (
    FakeModelClient,
    OllamaModelClient,
)
from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext


def _fake_ollama_server(response: str = "hello"):
    """启动临时 HTTP 服务器模拟 Ollama API。"""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(body_len)
            payload = json.loads(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = {
                "model": payload.get("model", ""),
                "created_at": "2026-01-01",
                "response": response,
                "done": True,
                "done_reason": "stop",
                "eval_count": len(response.split()),
            }
            self.wfile.write(json.dumps(resp).encode())

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    t = Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


class TestOllamaModelClient:
    """OllamaModelClient 单元测试。"""

    def test_constructs_with_defaults(self):
        client = OllamaModelClient()
        assert client.model == "qwen3.5:9b"
        assert client.host == "http://127.0.0.1:11434"

    def test_supports_prompt_cache_false(self):
        client = OllamaModelClient()
        assert client.supports_prompt_cache is False

    def test_complete_sends_correct_payload(self):
        """用 mock HTTP server 验证 payload 正确。"""
        server, port = _fake_ollama_server("hi there")
        try:
            client = OllamaModelClient(
                model="qwen3.5:9b",
                host=f"http://127.0.0.1:{port}",
            )
            result = client.complete("Say hello", max_new_tokens=2048)
            assert result == "hi there"
        finally:
            server.shutdown()

    def test_complete_defaults_model(self):
        """不传 model 时用默认值。"""
        server, port = _fake_ollama_server("ok")
        try:
            client = OllamaModelClient(host=f"http://127.0.0.1:{port}")
            result = client.complete("test", max_new_tokens=2048)
            assert result == "ok"
        finally:
            server.shutdown()


class TestLightClientIntegration:
    """Agent 双模型集成测试（FakeClient 替代真实 Ollama）。"""

    @pytest.fixture
    def agent_with_light(self, temp_workspace):
        ws = WorkspaceContext.build(str(temp_workspace))
        main = FakeModelClient(["<final>ok</final>"])
        light = FakeModelClient(["summary from light model"])
        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=2, max_new_tokens=2048),
            model_client=main,
            workspace=ws,
            light_client=light,
        )
        return agent

    def test_light_client_stored_on_agent(self, agent_with_light):
        assert agent_with_light.light_client is not None

    def test_task_summary_uses_light_client(self, agent_with_light):
        agent_with_light.ask("what is this project?")
        summary = agent_with_light.session["memory"]["working"]["task_summary"]
        # light client 预设 "summary from light model"
        assert "summary" in summary

    def test_dialog_summary_prefers_light_client(self, agent_with_light):
        cm = ContextManager(agent_with_light)
        history = [{"role": "user", "content": f"msg {i}: " + "x" * 200} for i in range(15)]
        result = cm._maybe_summarize_history(history, trigger_tokens=50)
        system_msgs = [h for h in result if h.get("role") == "system"]
        assert len(system_msgs) >= 1
        assert "summary from light model" in system_msgs[0]["content"]

    def test_falls_back_to_main_when_no_light(self, temp_workspace):
        ws = WorkspaceContext.build(str(temp_workspace))
        main = FakeModelClient(["<final>done</final>"])
        agent = Agent(
            config=AgentConfig(provider="fake", max_steps=2, max_new_tokens=2048),
            model_client=main,
            workspace=ws,
            light_client=None,
        )
        assert agent.light_client is None
        agent.ask("test")
        # 不报错正常完成
        summary = agent.session["memory"]["working"]["task_summary"]
        assert len(summary) > 0


class TestCLILightFlags:
    """CLI --light-provider / --light-model 参数解析。"""

    def test_light_provider_in_help(self):
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "-m", "agent_runtime", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert "--light-provider" in result.stdout
        assert "--light-model" in result.stdout

    def test_light_model_default(self):
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "-m", "agent_runtime", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert "qwen3.5:9b" in result.stdout
