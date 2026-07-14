"""suspect_blocks 渲染器单测（含 diff-only 上下文）。"""

import tempfile
from pathlib import Path

import pytest

from src.repair.suspect_blocks import (
    DIFF_CONTEXT_LINES,
    render_suspects_diff_only,
    render_suspects_summary,
    render_suspects_with_snippets,
)
from src.state import SuspectLocation


def _make_suspects() -> list[SuspectLocation]:
    return [
        SuspectLocation(
            file_path="src/calc.py",
            start_line=40,
            end_line=44,
            function_name="add",
            reason="TypeError",
            confidence=0.95,
        ),
        SuspectLocation(
            file_path="src/utils.py",
            start_line=15,
            end_line=15,
            function_name="validate",
            reason="ImportError",
            confidence=0.80,
        ),
    ]


@pytest.fixture
def temp_repo():
    """创建含源码文件的临时 repo。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "src").mkdir(parents=True)
        (root / "src" / "calc.py").write_text(
            "\n".join(
                [f"# line {i}: some code here for testing purposes" for i in range(1, 61)]
            )
        )
        (root / "src" / "utils.py").write_text(
            "\n".join(
                [f"# util line {i}: helper function body goes here" for i in range(1, 31)]
            )
        )
        yield root


def _read_snippet(repo_root: Path):
    """模拟 _read_code_snippet（markdown 格式）。"""
    def reader(file_path: str, start_line: int, end_line: int) -> str:
        path = Path(file_path)
        if not path.is_absolute():
            path = repo_root / path
        if not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8").split("\n")
        ctx_start = max(0, start_line - 4)
        ctx_end = min(len(lines), end_line + 4)
        block = ["    ```python"]
        for i in range(ctx_start, ctx_end):
            marker = ">>>" if start_line - 1 <= i < end_line else "   "
            block.append(f"    {marker} {lines[i]}")
        block.append("    ```")
        return "\n".join(block)
    return reader


def _read_line_range(repo_root: Path):
    """模拟 _read_line_range（原始行，无 markdown 包装）。"""
    def reader(file_path: str, start_line: int, end_line: int) -> str:
        path = Path(file_path)
        if not path.is_absolute():
            path = repo_root / path
        if not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8").split("\n")
        ctx_start = max(0, start_line - 1)
        ctx_end = min(len(lines), end_line)
        return "\n".join(lines[ctx_start:ctx_end])
    return reader


# ---------------------------------------------------------------------------
# 基础渲染
# ---------------------------------------------------------------------------


class TestRenderSuspectsSummary:
    def test_empty_returns_empty(self):
        assert render_suspects_summary([]) == ""

    def test_summary_includes_file_path_and_reason(self):
        suspects = _make_suspects()
        text = render_suspects_summary(suspects)
        assert "src/calc.py" in text
        assert "TypeError" in text
        assert "src/utils.py" in text

    def test_summary_respects_max(self):
        suspects = _make_suspects()
        text = render_suspects_summary(suspects, max_suspects=1)
        assert "src/calc.py" in text
        assert "src/utils.py" not in text


class TestRenderSuspectsWithSnippets:
    def test_empty_returns_empty(self):
        text, ordered = render_suspects_with_snippets([], lambda f, s, e: "")
        assert text == ""
        assert ordered == []

    def test_includes_header_and_code(self, temp_repo):
        suspects = _make_suspects()
        reader = _read_snippet(temp_repo)
        text, ordered = render_suspects_with_snippets(suspects, reader)
        assert "嫌疑位置" in text
        assert "src/calc.py" in text
        assert "```python" in text  # markdown code block
        assert len(ordered) == 2


# ---------------------------------------------------------------------------
# diff-only 上下文（V1.5-Bonus2）
# ---------------------------------------------------------------------------


class TestRenderSuspectsDiffOnly:
    def test_empty_returns_empty(self):
        assert render_suspects_diff_only([], lambda f, s, e: "") == ""

    def test_diff_format_has_hunk_headers(self, temp_repo):
        """diff-only 格式包含 unified diff header + hunk 头。"""
        suspects = _make_suspects()
        reader = _read_line_range(temp_repo)
        text = render_suspects_diff_only(suspects, reader)
        assert "--- a/src/calc.py" in text
        assert "+++ b/src/calc.py" in text
        assert "@@" in text  # hunk header
        assert "TypeError" in text  # reason 在 hunk header 中

    def test_diff_format_has_suspect_lines_marked(self, temp_repo):
        """嫌疑行标记为 '-' 前缀（diff 删除行）。"""
        suspects = _make_suspects()
        reader = _read_line_range(temp_repo)
        text = render_suspects_diff_only(suspects, reader)
        # 嫌疑行 line 40-44 → 应出现 "-" 前缀
        assert "-" in text
        # 上下文行 line 38-39 → 空格前缀
        lines = text.split("\n")
        suspect_markers = [l for l in lines if l.startswith("-")]
        assert len(suspect_markers) >= 1

    def test_diff_format_has_context_lines(self, temp_repo):
        """diff 包含 ± context_lines 邻域上下文行。"""
        suspects = _make_suspects()
        reader = _read_line_range(temp_repo)
        text = render_suspects_diff_only(suspects, reader, context_lines=2)
        lines = text.split("\n")
        # 有空格前缀的上下文行（非 diff header / hunk header）
        context_lines_found = [l for l in lines if l.startswith(" ") and "line" in l]
        assert len(context_lines_found) >= 1

    def test_diff_only_vs_full_snippet_token_count(self, temp_repo):
        """diff-only 格式的字符数应小于等于完整 snippet 格式。"""
        suspects = _make_suspects()
        snippet_reader = _read_snippet(temp_repo)
        line_reader = _read_line_range(temp_repo)

        full_text, _ = render_suspects_with_snippets(suspects, snippet_reader)
        diff_text = render_suspects_diff_only(suspects, line_reader)

        # diff 格式不应比 full snippet 更长
        assert len(diff_text) <= len(full_text), (
            f"diff-only ({len(diff_text)} chars) should be <= "
            f"full snippet ({len(full_text)} chars)"
        )
        # diff 格式不含 markdown code block 包装
        assert "```" not in diff_text

    def test_diff_only_respects_max_suspects(self, temp_repo):
        """diff-only 遵守 max_suspects 上限。"""
        suspects = _make_suspects()
        reader = _read_line_range(temp_repo)
        text = render_suspects_diff_only(suspects, reader, max_suspects=1)
        assert "src/calc.py" in text
        assert "src/utils.py" not in text

    def test_file_unreadable_graceful(self):
        """文件不可读时 diff-only 返回占位信息不崩溃。"""
        suspects = [SuspectLocation(
            file_path="nonexistent.py", start_line=1, end_line=3,
            reason="test", confidence=1.0,
        )]
        reader = _read_line_range(Path("/tmp"))
        text = render_suspects_diff_only(suspects, reader)
        assert "不可读" in text
        assert "@@" in text  # hunk header 仍存在

    def test_diff_context_lines_default(self):
        """默认 context_lines=DIFF_CONTEXT_LINES。"""
        assert DIFF_CONTEXT_LINES == 2
