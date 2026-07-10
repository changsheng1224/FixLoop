"""FakeModelClient + Agent.parse() 单测。"""

import pytest

from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent


class TestFakeModelClient:
    """FakeModelClient 预设输出序列测试。"""

    def test_returns_preset_sequence(self):
        client = FakeModelClient(["first", "second", "third"])
        assert client.complete("prompt1") == "first"
        assert client.complete("prompt2") == "second"
        assert client.complete("prompt3") == "third"

    def test_exhaustion_raises_error(self):
        client = FakeModelClient(["only"])
        client.complete("prompt")  # 消耗唯一输出
        with pytest.raises(RuntimeError, match="输出序列已耗尽"):
            client.complete("another prompt")

    def test_prompts_recorded(self):
        client = FakeModelClient(["a", "b"])
        client.complete("hello")
        client.complete("world")
        assert client.prompts == ["hello", "world"]
        assert len(client.prompts) == 2

    def test_supports_prompt_cache_false(self):
        client = FakeModelClient([])
        assert client.supports_prompt_cache is False


class TestAgentParse:
    """Agent.parse() 模型输出解析测试。"""

    def test_parse_json_tool(self):
        kind, payload = Agent.parse(
            '<tool>{"name":"read_file","args":{"path":"src/main.py"}}</tool>'
        )
        assert kind == "tool"
        assert payload["name"] == "read_file"
        assert payload["args"]["path"] == "src/main.py"

    def test_parse_xml_tool_with_body(self):
        raw = '<tool name="write_file" path="fix.py">\n<content>print("hello")</content>\n</tool>'
        kind, payload = Agent.parse(raw)
        assert kind == "tool"
        assert payload["name"] == "write_file"
        assert "print" in payload["body"]

    def test_parse_final(self):
        kind, text = Agent.parse("<final>问题是缺少类型转换</final>")
        assert kind == "final"
        assert "类型转换" in text

    def test_parse_final_multiline(self):
        kind, text = Agent.parse(
            "<final>\n修复完成：\n- 修改了 calculator.py\n- 测试通过\n</final>"
        )
        assert kind == "final"
        assert "calculator.py" in text

    def test_parse_empty_input(self):
        kind, notice = Agent.parse("")
        assert kind == "retry"
        assert "解析失败" in str(notice)

    def test_parse_garbage_text(self):
        kind, notice = Agent.parse("random text without any tags")
        assert kind == "retry"
        assert "解析失败" in str(notice)

    def test_parse_invalid_json_in_tool(self):
        kind, notice = Agent.parse("<tool>{not valid json}</tool>")
        assert kind == "retry"
        assert "^" in str(notice)

    def test_parse_partial_tool_tag(self):
        """未闭合的 tool 标签不匹配，应视为 retry。"""
        kind, notice = Agent.parse('<tool>{"name":"test"')
        assert kind == "retry"
        assert "解析失败" in str(notice)
