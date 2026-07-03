"""AnthropicCompatibleModelClient 集成测试。

调用真实 DeepSeek API，验证连通性和响应格式。
如未配置 DEEPSEEK_API_KEY，测试自动跳过。
"""

import os
import re

import pytest

from agent_runtime.providers.clients import AnthropicCompatibleModelClient


# 从环境变量或 .env 文件加载 API key
def _load_api_key() -> str:
    """尝试从环境变量或 .env 文件加载 DeepSeek API Key。"""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key and key != "sk-your-key-here":
        return key

    # 尝试读取 .env 文件
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DEEPSEEK_API_KEY="):
                    val = line.split("=", 1)[1].strip()
                    if val and val != "sk-your-key-here":
                        return val
    except FileNotFoundError:
        pass
    return ""


API_KEY = _load_api_key()
REQUIRES_API = pytest.mark.skipif(
    not API_KEY,
    reason="需要配置 DEEPSEEK_API_KEY（在 .env 中设置有效的 API Key）",
)


@pytest.fixture
def client():
    """创建 AnthropicCompatibleModelClient 实例。"""
    return AnthropicCompatibleModelClient(
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic/v1"),
        api_key=API_KEY,
        temperature=0.2,
        timeout=30,
    )


class TestAnthropicClientIntegration:
    """真实 API 集成测试。"""

    @REQUIRES_API
    def test_simple_completion(self, client):
        """发送简单 prompt，验证返回非空文本。"""
        # DeepSeek v4 含 thinking tokens，需 >= 100 才够输出文本
        result = client.complete("Say 'hello' in one word.", max_new_tokens=200)
        assert result is not None
        assert len(result.strip()) > 0
        assert isinstance(result, str)

    @REQUIRES_API
    def test_returns_chinese_text(self, client):
        """发送中文 prompt，验证返回中文响应。"""
        # 中文 token 密度低 + thinking tokens，需较大限额
        result = client.complete("请用一句话介绍 Python。", max_new_tokens=800)
        assert len(result.strip()) > 0
        # 至少包含一些中文字符
        has_chinese = bool(re.search(r"[一-鿿]", result))
        assert has_chinese, f"期望中文响应，实际: {result[:100]}"


class TestExtractText:
    """_extract_text 单元测试。"""

    def test_anthropic_format(self):
        client = AnthropicCompatibleModelClient(model="test", base_url="http://x", api_key="x")
        data = {
            "content": [
                {"type": "text", "text": "Hello "},
                {"type": "text", "text": "World"},
            ]
        }
        assert client._extract_text(data) == "Hello World"

    def test_openai_format(self):
        client = AnthropicCompatibleModelClient(model="test", base_url="http://x", api_key="x")
        data = {"choices": [{"message": {"content": "response text"}}]}
        assert client._extract_text(data) == "response text"

    def test_empty_response(self):
        client = AnthropicCompatibleModelClient(model="test", base_url="http://x", api_key="x")
        assert client._extract_text({}) == ""
