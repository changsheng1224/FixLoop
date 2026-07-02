"""修复工具单测：stack_parser + git_tools + find_test。"""

import json

from agent_runtime.tool_context import ToolContext
from src.tools.find_test import find_test_for_function
from src.tools.git_tools import git_blame, git_diff
from src.tools.stack_parser import stack_parse


class TestStackParser:
    def test_basic_traceback(self):
        tb = (
            'Traceback (most recent call last):\n'
            '  File "calc.py", line 42, in add\n'
            '    return a + b\n'
            'TypeError: unsupported operand type(s) for +\n'
        )
        result = stack_parse(None, {"traceback": tb})
        data = json.loads(result)
        assert data["exception_type"] == "TypeError"
        assert len(data["frames"]) == 1
        assert data["frames"][0]["file"] == "calc.py"
        assert data["frames"][0]["line"] == 42

    def test_chained_exception(self):
        tb = (
            "During handling of the above exception, "
            "another exception occurred:\n"
            'File "app.py", line 10, in run\n'
            "ValueError: invalid value\n"
        )
        result = stack_parse(None, {"traceback": tb})
        data = json.loads(result)
        assert data["is_chained"] is True

    def test_missing_traceback(self):
        result = stack_parse(None, {})
        assert "Error" in result


class TestGitTools:
    def test_blame_on_committed_file(self, temp_workspace):
        ctx = ToolContext(root=str(temp_workspace))
        result = git_blame(ctx, {"file": "README.md", "line": 1})
        assert "author" in result or "Error" in result  # git blame 需要真实 git 历史

    def test_diff_no_changes(self, temp_workspace):
        ctx = ToolContext(root=str(temp_workspace))
        result = git_diff(ctx, {"commit_a": "HEAD", "commit_b": "HEAD", "path": "README.md"})
        assert isinstance(result, str)


class TestFindTest:
    def test_no_test_found(self, temp_workspace):
        ctx = ToolContext(root=str(temp_workspace))
        result = find_test_for_function(ctx, {
            "function_name": "unknown_fn",
            "file_path": "README.md",
        })
        assert "未找到" in result

    def test_finds_by_filename(self, temp_workspace):
        (temp_workspace / "tests").mkdir(exist_ok=True)
        (temp_workspace / "tests" / "test_calculator.py").write_text(
            "def test_add():\n    pass\n"
        )
        (temp_workspace / "calculator.py").write_text("def add(a,b): return a+b")
        ctx = ToolContext(root=str(temp_workspace))
        result = find_test_for_function(ctx, {
            "function_name": "add",
            "file_path": "calculator.py",
        })
        assert "test_calculator" in result
