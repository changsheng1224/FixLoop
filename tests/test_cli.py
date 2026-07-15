"""CLI 模块测试：_load_dotenv, _build_model_client, _build_light_client。"""

import os
import tempfile
from pathlib import Path

from agent_runtime.config import AgentConfig


class TestLoadDotenv:
    """_load_dotenv 测试。"""

    def test_loads_env_file(self):
        from agent_runtime.cli import _load_dotenv

        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("MY_TEST_KEY=hello123\n")
            old_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                _load_dotenv()
                assert os.environ["MY_TEST_KEY"] == "hello123"
            finally:
                os.chdir(old_cwd)
                os.environ.pop("MY_TEST_KEY", None)

    def test_missing_env_no_error(self):
        from agent_runtime.cli import _load_dotenv

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                _load_dotenv()
            finally:
                os.chdir(old_cwd)


class TestBuildModelClient:
    """_build_model_client 测试。"""

    def test_fake_provider(self):
        from agent_runtime.cli import _build_model_client

        class Args:
            provider = "fake"
            api_key = None
            base_url = None

        client = _build_model_client(Args(), AgentConfig())
        assert hasattr(client, "complete")

    def test_deepseek_from_env(self):
        from agent_runtime.cli import _build_model_client

        os.environ["DEEPSEEK_API_KEY"] = "sk-test-123"
        os.environ["DEEPSEEK_BASE_URL"] = "https://test.api/v1"

        class Args:
            provider = "deepseek"
            api_key = None
            base_url = None

        try:
            client = _build_model_client(Args(), AgentConfig(model="deepseek-v4-pro"))
            assert client.api_key == "sk-test-123"
            assert client.base_url == "https://test.api/v1"
        finally:
            del os.environ["DEEPSEEK_API_KEY"]
            del os.environ["DEEPSEEK_BASE_URL"]

    def test_api_key_from_args(self):
        from agent_runtime.cli import _build_model_client

        class Args:
            provider = "deepseek"
            api_key = "sk-args-key"
            base_url = None

        client = _build_model_client(Args(), AgentConfig(model="deepseek-v4-pro"))
        assert client.api_key == "sk-args-key"

    def test_openai_provider_from_cli(self):
        from agent_runtime.cli import _build_model_client
        from agent_runtime.providers.clients import OpenAICompatibleModelClient

        class Args:
            provider = "openai"
            api_key = "sk-openai"
            base_url = "https://openai.test/v1"

        client = _build_model_client(Args(), AgentConfig(provider="openai", model="gpt-test"))

        assert isinstance(client, OpenAICompatibleModelClient)
        assert client.model == "gpt-test"
        assert client.base_url == "https://openai.test/v1"


class TestBuildLightClient:
    """_build_light_client 测试。"""

    def test_none_when_no_light_provider(self):
        from agent_runtime.cli import _build_light_client

        class Args:
            light_provider = None
            light_model = "qwen3.5:9b"

        assert _build_light_client(Args()) is None

    def test_ollama_light_client(self):
        from agent_runtime.cli import _build_light_client

        class Args:
            light_provider = "ollama"
            light_model = "qwen3:1.8b"

        client = _build_light_client(Args())
        assert client is not None
        assert client.model == "qwen3:1.8b"


class TestLayerBoundary:
    """Layer 1 CLI 不应反向依赖 Layer 2 src 包。"""

    def test_agent_runtime_cli_does_not_import_src(self):
        text = Path("agent_runtime/cli.py").read_text(encoding="utf-8")
        assert "from src." not in text
        assert "import src." not in text
