"""bootstrap 模块测试：load_dotenv, create_model_client。"""

import os
import tempfile
from pathlib import Path

from agent_runtime.bootstrap import create_model_client, load_dotenv
from agent_runtime.providers.clients import (
    AnthropicCompatibleModelClient,
    FakeModelClient,
    OllamaModelClient,
    OpenAICompatibleModelClient,
)


class TestBootstrapLoadDotenv:
    def test_loads_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("BOOTSTRAP_TEST_KEY=hello123\n")
            load_dotenv(Path(tmp))
            assert os.environ["BOOTSTRAP_TEST_KEY"] == "hello123"
            os.environ.pop("BOOTSTRAP_TEST_KEY", None)

    def test_missing_env_no_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            load_dotenv(Path(tmp))


class TestBootstrapCreateModelClient:
    def test_inject_client(self):
        sentinel = object()
        assert create_model_client(sentinel) is sentinel

    def test_fake_provider(self):
        client = create_model_client(provider="fake")
        assert isinstance(client, FakeModelClient)

    def test_from_env(self):
        os.environ["DEEPSEEK_API_KEY"] = "sk-bootstrap"
        os.environ["DEEPSEEK_BASE_URL"] = "https://bootstrap.test/v1"
        try:
            client = create_model_client(model="deepseek-v4-pro")
            assert isinstance(client, AnthropicCompatibleModelClient)
            assert client.api_key == "sk-bootstrap"
            assert client.base_url == "https://bootstrap.test/v1"
        finally:
            del os.environ["DEEPSEEK_API_KEY"]
            del os.environ["DEEPSEEK_BASE_URL"]

    def test_openai_provider_uses_openai_client(self):
        client = create_model_client(
            provider="openai",
            model="gpt-test",
            base_url="https://openai.test/v1",
            api_key="sk-openai",
            temperature=0.7,
        )

        assert isinstance(client, OpenAICompatibleModelClient)
        assert client.model == "gpt-test"
        assert client.base_url == "https://openai.test/v1"
        assert client.api_key == "sk-openai"
        assert client.temperature == 0.7

    def test_ollama_provider_uses_ollama_client(self):
        client = create_model_client(
            provider="ollama",
            model="qwen-test",
            base_url="http://127.0.0.1:11434",
            temperature=0.4,
        )

        assert isinstance(client, OllamaModelClient)
        assert client.model == "qwen-test"
        assert client.host == "http://127.0.0.1:11434"
        assert client.temperature == 0.4
