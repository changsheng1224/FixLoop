"""bootstrap 模块测试：load_dotenv, create_model_client。"""

import os
import tempfile
from pathlib import Path

from agent_runtime.bootstrap import create_model_client, load_dotenv
from agent_runtime.providers.clients import AnthropicCompatibleModelClient, FakeModelClient


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
