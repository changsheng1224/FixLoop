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
        assert "④" in prompt
        assert "^" in prompt
        assert "Expecting" in prompt
        assert "②" in prompt

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
        failure = diagnose_parse_failure("<read_file>src/main.py</read_file>")
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


# ---------------------------------------------------------------------------
# Retry prompt 四段式（V1.5-Bonus1d）
# ---------------------------------------------------------------------------


class TestFourSectionPrompt:
    def test_all_four_sections_present(self):
        """四段 prompt 包含所有 section 标题。"""
        from agent_runtime.parse_recovery import (
            ParseFailure,
            build_recovery_prompt,
        )

        failure = ParseFailure(
            kind="json_in_tool",
            snippet='{"name": "read_file", "args": {',
            error_offset=30,
            error_message="Expecting value (col 31)",
            hint="JSON 格式错误",
        )
        prompt = build_recovery_prompt(
            failure,
            last_tool_call={"name": "read_file", "args": {"path": "app.py"}},
        )
        assert "①" in prompt
        assert "②" in prompt
        assert "③" in prompt
        assert "④" in prompt

    def test_includes_last_tool_name(self):
        """四段 prompt 的 section ① 包含上次 tool 名。"""
        from agent_runtime.parse_recovery import (
            ParseFailure,
            build_recovery_prompt,
        )

        failure = ParseFailure(
            kind="json_in_tool",
            snippet="bad json",
            error_offset=0,
            error_message="err",
            hint="hint",
        )
        prompt = build_recovery_prompt(
            failure,
            last_tool_call={"name": "read_file", "args": {"path": "app.py", "start": 1}},
        )
        assert "read_file" in prompt
        assert "app.py" in prompt

    def test_no_last_tool_skips_section_one(self):
        """last_tool_call=None 时不添加 section ①。"""
        from agent_runtime.parse_recovery import (
            ParseFailure,
            build_recovery_prompt,
        )

        failure = ParseFailure(
            kind="empty",
            snippet="",
            error_offset=None,
            error_message="empty",
            hint="empty",
        )
        prompt = build_recovery_prompt(failure, last_tool_call=None)
        assert "①" not in prompt  # section ① 完全跳过

    def test_section_two_has_truncated_output(self):
        """Section ② 包含截断的原始输出。"""
        from agent_runtime.parse_recovery import (
            ParseFailure,
            build_recovery_prompt,
        )

        long_text = "x" * 800
        failure = ParseFailure(
            kind="unrecognized",
            snippet=long_text,
            error_offset=None,
            error_message="bad",
            hint="bad",
        )
        prompt = build_recovery_prompt(failure)
        assert "x" * 600 in prompt  # 截断到 600

    def test_section_three_has_caret(self):
        """Section ③ 包含 caret 定位。"""
        from agent_runtime.parse_recovery import (
            ParseFailure,
            build_recovery_prompt,
        )

        failure = ParseFailure(
            kind="json_in_tool",
            snippet='{"name": "read_file", "args": {extra',
            error_offset=35,
            error_message="Extra data",
            hint="JSON error",
        )
        prompt = build_recovery_prompt(failure)
        assert "③" in prompt
        assert "Extra data" in prompt

    def test_section_four_has_format_example(self):
        """Section ④ 包含格式示例。"""
        from agent_runtime.parse_recovery import (
            ParseFailure,
            build_recovery_prompt,
        )

        for kind in ("json_in_tool", "wrong_xml_tag", "unclosed_tag", "empty", "unrecognized"):
            failure = ParseFailure(
                kind=kind,
                snippet="x",
                error_offset=0,
                error_message="err",
                hint="hint",
            )
            prompt = build_recovery_prompt(failure)
            assert "④" in prompt, f"kind={kind} missing section ④"

    def test_parse_retry_has_tool_anchor(self):
        """ParseRetry.has_last_tool_anchor 正确反映 last_tool_call 存在性。"""
        from agent_runtime.parse_recovery import make_parse_retry

        retry_with = make_parse_retry(
            "bad json",
            last_tool_call={"name": "grep", "args": {"pattern": "TODO"}},
        )
        assert retry_with.has_last_tool_anchor is True

        retry_without = make_parse_retry("bad json")
        assert retry_without.has_last_tool_anchor is False
