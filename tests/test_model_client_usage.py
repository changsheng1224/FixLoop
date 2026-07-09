"""AnthropicCompatibleModelClient usage 记录测试。"""

from agent_runtime.providers.clients import AnthropicCompatibleModelClient


class TestAnthropicRecordUsage:
    def test_record_usage_accumulates_cache(self):
        client = AnthropicCompatibleModelClient(
            model="deepseek-chat",
            base_url="https://example.com",
            api_key="test",
        )
        client._record_usage(
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 70,
                "cache_creation_input_tokens": 30,
            }
        )
        assert client.session_usage["input_tokens"] == 100
        assert client.session_usage["output_tokens"] == 20
        assert client.session_usage["cache_read_tokens"] == 70
        assert client.session_usage["cache_creation_tokens"] == 30
        assert client.session_usage["calls"] == 1
