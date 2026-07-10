"""parse_recovery：解析失败 recovery prompt 单测。"""

import json

import pytest

from agent_runtime.parse_recovery import (
    ParseFailure,
    build_recovery_prompt,
    diagnose_parse_failure,
    failure_from_json_in_tool,
    format_caret_line,
    truncate_snippet,
)
from agent_runtime.runtime import Agent


class TestCaretFormatting:
    def test_format_caret_line(self):
        assert format_caret_line('{"a":1}', 6) == "      ^"

    def test_truncate_snippet(self):
        long = "x" * 600
        assert len(truncate_snippet(long, max_chars=500)) == 500


class TestBuildRecoveryPrompt:
    def test_includes_caret_and_error(self):
        failure = ParseFailure(
            kind="json_in_tool",
            snippet='{"name":"x"',
            error_offset=11,
            error_message="Expecting ',' delimiter（column 12）",
            hint="<tool> 内必须是合法 JSON",
        )
        prompt = build_recovery_prompt(failure)
        assert "解析失败" in prompt
        assert "^" in prompt
        assert "Expecting" in prompt
        assert "片段" in prompt

    def test_empty_snippet_omits_caret(self):
        failure = ParseFailure(
            kind="empty",
            snippet="",
            error_offset=None,
            error_message="模型返回空输出",
            hint="请输出 <tool> 或 <final>",
        )
        prompt = build_recovery_prompt(failure)
        assert "^" not in prompt
        assert "空输出" in prompt


class TestDiagnoseParseFailure:
    def test_json_in_tool(self):
        raw = '<tool>{"name":"read_file","args":{"path": "main.py}</tool>'
        failure = diagnose_parse_failure(raw)
        assert failure.kind == "json_in_tool"
        assert failure.error_offset is not None
        assert "read_file" in failure.snippet

    def test_wrong_xml_tag(self):
        failure = diagnose_parse_failure('<read_file>src/main.py</read_file>')
        assert failure.kind == "wrong_xml_tag"
        assert failure.error_offset == 0
        assert "read_file" in failure.error_message

    def test_unclosed_tool_tag(self):
        failure = diagnose_parse_failure('<tool>{"name":"test"')
        assert failure.kind == "unclosed_tag"
        assert "<tool>" in failure.snippet or "name" in failure.snippet

    def test_empty(self):
        failure = diagnose_parse_failure("")
        assert failure.kind == "empty"

    def test_unrecognized(self):
        failure = diagnose_parse_failure("random text without any tags")
        assert failure.kind == "unrecognized"
        assert "random" in failure.snippet


class TestFailureFromJsonInTool:
    def test_uses_json_decode_error_pos(self):
        text = "{not valid json}"
        with pytest.raises(json.JSONDecodeError) as caught:
            json.loads(text)
        failure = failure_from_json_in_tool(text, caught.value)
        assert failure.kind == "json_in_tool"
        assert failure.error_offset == caught.value.pos


class TestAgentParseRecovery:
    def test_invalid_json_in_tool_has_caret(self):
        kind, notice = Agent.parse("<tool>{not valid json}</tool>")
        assert kind == "retry"
        assert "^" in str(notice)
        assert "JSON" in str(notice) or "json" in str(notice).lower()

    def test_garbage_still_retries(self):
        kind, notice = Agent.parse("random text without any tags")
        assert kind == "retry"
        assert "解析失败" in str(notice)

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "random text without any tags",
            "<tool>{not valid json}</tool>",
            '<tool>{"name":"test"',
            "<read_file>x</read_file>",
        ],
    )
    def test_retry_payload_is_parse_retry_with_failure(self, raw):
        from agent_runtime.parse_recovery import ParseRetry, diagnose_parse_failure

        kind, payload = Agent.parse(raw)
        assert kind == "retry"
        assert isinstance(payload, ParseRetry)
        assert payload.failure.kind == diagnose_parse_failure(raw).kind
