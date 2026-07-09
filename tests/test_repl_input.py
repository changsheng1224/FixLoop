"""L1 REPL 多行输入（\\ 续行）单测。"""

import pytest

from agent_runtime.repl_input import line_ends_with_continuation, read_repl_input


class TestLineEndsWithContinuation:
    def test_no_backslash(self):
        assert line_ends_with_continuation("hello") is False

    def test_single_backslash(self):
        assert line_ends_with_continuation("hello\\") is True

    def test_double_backslash_literal(self):
        assert line_ends_with_continuation("hello\\\\") is False

    def test_triple_backslash_continues(self):
        assert line_ends_with_continuation("hello\\\\\\") is True

    def test_strips_cr_before_check(self):
        assert line_ends_with_continuation("line\\\r") is True


class TestReadReplInput:
    @staticmethod
    def _reader_from_lines(*lines: str):
        it = iter(lines)

        def reader(_prompt: str) -> str:
            return next(it)

        return reader

    def test_single_line(self):
        assert read_repl_input(reader=self._reader_from_lines("hello world")) == "hello world"

    def test_two_line_continuation(self):
        reader = self._reader_from_lines("first line\\", "second line")
        assert read_repl_input(reader=reader) == "first line\nsecond line"

    def test_three_line_continuation(self):
        reader = self._reader_from_lines("a\\", "b\\", "c")
        assert read_repl_input(reader=reader) == "a\nb\nc"

    def test_double_backslash_not_continuation(self):
        reader = self._reader_from_lines("path\\\\", "should not read")
        assert read_repl_input(reader=reader) == "path\\\\"

    def test_slash_command_disables_continuation(self):
        reader = self._reader_from_lines("/help\\", "ignored line")
        assert read_repl_input(reader=reader) == "/help\\"

    def test_preserves_empty_continuation_line(self):
        # 中间空行需用单独 "\\" 续行行显式表达
        reader = self._reader_from_lines("line1\\", "\\", "line3")
        assert read_repl_input(reader=reader) == "line1\n\nline3"

    def test_preserves_inner_spaces(self):
        reader = self._reader_from_lines("  indented\\", "  more  ")
        assert read_repl_input(reader=reader) == "  indented\n  more  "

    def test_eof_on_first_line_raises(self):
        def _eof(_prompt):
            raise EOFError

        with pytest.raises(EOFError):
            read_repl_input(reader=_eof)

    def test_eof_on_continuation_raises(self):
        def _reader(prompt):
            if prompt.strip() == ">":
                return "go\\"
            raise EOFError

        with pytest.raises(EOFError):
            read_repl_input(reader=_reader)
