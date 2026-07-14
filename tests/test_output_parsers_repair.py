"""多级 parse 修复单测：trailing comma + 注释兼容。"""

import json

from src.repair.output_parsers import (
    _repair_trailing_comma,
    _strip_json_comments,
    _load_json,
)


class TestTrailingCommaRepair:
    def test_trailing_comma_in_object(self):
        """对象尾随逗号修复。"""
        text = '{"name": "test", "value": 42,}'
        repaired = _repair_trailing_comma(text)
        data = json.loads(repaired)
        assert data["name"] == "test"

    def test_trailing_comma_in_array(self):
        """数组尾随逗号修复。"""
        text = '[1, 2, 3,]'
        repaired = _repair_trailing_comma(text)
        data = json.loads(repaired)
        assert data == [1, 2, 3]

    def test_trailing_comma_multiline(self):
        """多行尾随逗号。"""
        text = '{\n  "a": 1,\n  "b": 2,\n}'
        repaired = _repair_trailing_comma(text)
        data = json.loads(repaired)
        assert data == {"a": 1, "b": 2}

    def test_no_trailing_comma_unchanged(self):
        """无尾随逗号不修改。"""
        text = '{"x": 1, "y": 2}'
        repaired = _repair_trailing_comma(text)
        assert repaired == text


class TestStripJsonComments:
    def test_line_comment(self):
        text = '{\n  // this is a comment\n  "key": "val"\n}'
        stripped = _strip_json_comments(text)
        assert "//" not in stripped
        assert "key" in stripped

    def test_block_comment(self):
        text = '{\n  /* block comment */\n  "key": "val"\n}'
        stripped = _strip_json_comments(text)
        assert "/*" not in stripped
        assert "key" in stripped


class TestLoadJsonWithRepair:
    def test_strict_json_still_works(self):
        data = _load_json('[{"file_path": "a.py", "start_line": 1, "end_line": 2}]')
        assert isinstance(data, list)
        assert len(data) == 1

    def test_trailing_comma_parsed(self):
        """带尾随逗号的 JSON 可解析。"""
        text = '[{"file_path": "a.py", "start_line": 1, "end_line": 2,}]'
        data = _load_json(text)
        assert isinstance(data, list)
        assert data[0]["file_path"] == "a.py"

    def test_json_with_comment_parsed(self):
        """带注释的 JSON 可解析。"""
        text = '// suspects list\n[{"file_path": "a.py", "start_line": 1, "end_line": 2}]'
        data = _load_json(text)
        assert isinstance(data, list)
        assert data[0]["file_path"] == "a.py"

    def test_code_block_extracted_and_repaired(self):
        """markdown code block 中的 trailing comma JSON 可解析。"""
        text = '```json\n[{"file_path": "a.py", "start_line": 1, "end_line": 2,}]\n```'
        data = _load_json(text)
        assert isinstance(data, list)

    def test_broken_json_returns_none(self):
        """无法修复的 JSON 返回 None。"""
        assert _load_json("not json at all [{") is None
