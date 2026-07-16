"""Property-based JSON fuzz 单测：畸形串不崩溃。

依赖: hypothesis（仅 dev），未安装时自动 skip。
"""

import pytest

try:
    from hypothesis import given, settings
    from hypothesis.strategies import text

    HAS_HYPOTHESIS = True
except ImportError:
    HAS_HYPOTHESIS = False

from src.repair.output_parsers import (
    _load_json,
    _repair_trailing_comma,
    _strip_json_comments,
    parse_retrieved_context,
    parse_suspect_list,
    parse_verification,
)

# hypothesis 导入后动态创建 fuzz 类（避免装饰器在导入失败时执行）
if HAS_HYPOTHESIS:

    class TestFuzzParsers:
        @given(text())
        @settings(max_examples=200)
        def test_load_json_never_crashes(self, s):
            result = _load_json(s)
            assert result is None or isinstance(result, dict | list)

        @given(text())
        @settings(max_examples=200)
        def test_parse_suspect_list_never_crashes(self, s):
            result = parse_suspect_list(s)
            assert isinstance(result, list)

        @given(text())
        @settings(max_examples=200)
        def test_parse_retrieved_context_never_crashes(self, s):
            result = parse_retrieved_context(s)
            assert result is not None

        @given(text())
        @settings(max_examples=200)
        def test_parse_verification_never_crashes(self, s):
            result = parse_verification(s)
            assert result is not None

        @given(text())
        @settings(max_examples=200)
        def test_repair_and_strip_never_crash(self, s):
            r1 = _repair_trailing_comma(s)
            r2 = _strip_json_comments(s)
            assert isinstance(r1, str)
            assert isinstance(r2, str)


@pytest.mark.skipif(HAS_HYPOTHESIS, reason="hypothesis 已安装，无需跑 basic")
class TestBasicFuzzFallback:
    """hypothesis 不可用时的兜底 fuzzy 测试。"""

    def test_malformed_inputs_dont_crash(self):
        broken = [
            "{",
            "}",
            "[}",
            "{]",
            "'",
            "\x00\x01\x02",
            "null",
            "undefined",
            "NaN",
            "Infinity",
            "function(){}",
            "while(true){}",
            "{{{{{{{{{",
            "}}}}}}}}}",
            '{"a": 1, "b": }',
            "[1, 2, ]",
            "a" * 10000,  # 超长串
            "\n" * 1000,
        ]
        for s in broken:
            _load_json(s)
            parse_suspect_list(s)
            parse_retrieved_context(s)
            parse_verification(s)


class TestSuspectListToolCallFallback:
    def test_extracts_path_from_xml_tool_call_when_json_is_missing(self):
        raw = (
            "<function_calls>"
            '<invoke name="inspect_file">'
            '<parameter name="path">calc.py</parameter>'
            "</invoke>"
            "</function_calls>"
        )

        suspects = parse_suspect_list(raw)

        assert len(suspects) == 1
        assert suspects[0].file_path == "calc.py"
        assert suspects[0].reason == "tool_call_fallback"
        assert suspects[0].confidence == 0.3

    def test_extracts_path_from_parameter_with_attributes(self):
        raw = (
            "<function_calls>"
            '<invoke name="read_file">'
            '<parameter name="path" string="true">pkg/values.py</parameter>'
            "</invoke>"
            "</function_calls>"
        )

        suspects = parse_suspect_list(raw)

        assert [s.file_path for s in suspects] == ["pkg/values.py"]
