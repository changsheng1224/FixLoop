"""修复工具注册表：build_repair_tools() 统一注册 M5 新工具。

遵循 M1 的注册表模式：{name: {schema, risky, description, run}}。
"""

from agent_runtime.schema_utils import auto_schema
from agent_runtime.tools import TIER_HOST, GrepArgs, tool_grep
from src.tools.ast_parser import AstParseArgs, ast_parse
from src.tools.find_test import FindTestArgs, find_test_for_function
from src.tools.git_tools import (
    GitBlameArgs,
    GitDiffArgs,
    git_blame,
    git_diff,
)
from src.tools.stack_parser import StackParseArgs, stack_parse


def build_repair_tools(context) -> dict:
    """构建修复工具注册表。

    Args:
        context: ToolContext 实例。

    Returns:
        工具注册表字典。
    """
    registry = {}

    # ---- grep (L2 visible) ----
    registry["grep"] = {
        "schema": auto_schema(GrepArgs),
        "risky": False,
        "execution_tier": TIER_HOST,
        "description": "内容搜索（rg 优先，Python fallback）。参数: pattern, path, glob, ignore_case, context_lines, max_results",
        "run": lambda args: tool_grep(context, args),
    }

    # ---- ast_parse ----
    registry["ast_parse"] = {
        "schema": auto_schema(AstParseArgs),
        "risky": False,
        "execution_tier": TIER_HOST,
        "description": "解析 Python 文件为结构化函数/类/方法列表（排除注释）。参数: path",
        "run": lambda args: ast_parse(context, args),
    }

    # ---- stack_parse ----
    registry["stack_parse"] = {
        "schema": auto_schema(StackParseArgs),
        "risky": False,
        "execution_tier": TIER_HOST,
        "description": "解析 Python Traceback 为结构化数据。参数: traceback",
        "run": lambda args: stack_parse(context, args),
    }

    # ---- git_blame ----
    registry["git_blame"] = {
        "schema": auto_schema(GitBlameArgs),
        "risky": False,
        "execution_tier": TIER_HOST,
        "description": "查看指定文件指定行的最后修改者。参数: file, line",
        "run": lambda args: git_blame(context, args),
    }

    # ---- git_diff ----
    registry["git_diff"] = {
        "schema": auto_schema(GitDiffArgs),
        "risky": False,
        "execution_tier": TIER_HOST,
        "description": "查看两个 commit 之间的文件级差异。参数: commit_a, commit_b, path",
        "run": lambda args: git_diff(context, args),
    }

    # ---- find_test ----
    registry["find_test"] = {
        "schema": auto_schema(FindTestArgs),
        "risky": False,
        "execution_tier": TIER_HOST,
        "description": "定位函数的对应测试文件与用例。参数: function_name, file_path",
        "run": lambda args: find_test_for_function(context, args),
    }

    return registry
