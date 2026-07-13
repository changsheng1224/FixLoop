"""修复工具单测：stack_parser + git_tools + find_test + registry。"""

import json

from agent_runtime.tool_context import ToolContext
from src.tools.find_test import find_test_for_function
from src.tools.git_tools import git_blame, git_diff
from src.tools.registry import build_repair_tools
from src.tools.stack_parser import stack_parse


class TestStackParser:
    def test_basic_traceback(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "calc.py", line 42, in add\n'
            "    return a + b\n"
            "TypeError: unsupported operand type(s) for +\n"
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
        result = find_test_for_function(
            ctx,
            {
                "function_name": "unknown_fn",
                "file_path": "README.md",
            },
        )
        assert "未找到" in result

    def test_finds_by_filename(self, temp_workspace):
        (temp_workspace / "tests").mkdir(exist_ok=True)
        (temp_workspace / "tests" / "test_calculator.py").write_text("def test_add():\n    pass\n")
        (temp_workspace / "calculator.py").write_text("def add(a,b): return a+b")
        ctx = ToolContext(root=str(temp_workspace))
        result = find_test_for_function(
            ctx,
            {
                "function_name": "add",
                "file_path": "calculator.py",
            },
        )
        assert "test_calculator" in result


class TestRepairRegistry:
    def test_all_tools_registered(self, temp_workspace):
        ctx = ToolContext(root=str(temp_workspace))
        registry = build_repair_tools(ctx)
        assert set(registry.keys()) == {
            "grep",
            "ast_parse",
            "java_ast_parse",
            "java_stack_parse",
            "stack_parse",
            "git_blame",
            "git_diff",
            "find_test",
        }

    def test_tools_runnable(self, temp_workspace):
        ctx = ToolContext(root=str(temp_workspace))
        registry = build_repair_tools(ctx)
        r = registry["ast_parse"]["run"]({"path": "README.md"})
        assert isinstance(r, str)

    def test_all_repair_tools_are_host_tier(self, temp_workspace):
        ctx = ToolContext(root=str(temp_workspace))
        registry = build_repair_tools(ctx)
        for name in ("ast_parse", "stack_parse", "git_blame", "git_diff", "find_test"):
            spec = registry.get(name)
            assert spec is not None
            assert spec.get("execution_tier") == "host", f"{name} should be host tier"


# ---------------------------------------------------------------------------
# L2 registry 与 auto_schema 一致性（V1.4-Bonus10c）
# ---------------------------------------------------------------------------


class TestRegistrySchemaConsistency:
    def test_all_canonical_tools_registered(self):
        """REPAIR_CANONICAL_TOOL_NAMES 中的所有工具都已注册。"""
        from src.tools.composite import REPAIR_CANONICAL_TOOL_NAMES, build_repair_canonical_tools

        ctx = ToolContext(root=".")
        tools = build_repair_canonical_tools(ctx)
        registered = set(tools.keys())
        missing = set(REPAIR_CANONICAL_TOOL_NAMES) - registered
        assert not missing, f"未注册的 canonical 工具: {missing}"

    def test_repair_tools_have_required_fields(self):
        """build_repair_tools 的每个条目含必需字段。"""
        ctx = ToolContext(root=".")
        tools = build_repair_tools(ctx)
        required = {"schema", "risky", "execution_tier", "description", "run"}
        for name, spec in tools.items():
            missing = required - set(spec.keys())
            assert not missing, f"{name}: 缺少字段 {missing}"

    def test_schema_matches_auto_schema(self):
        """手动声明的 schema 与 auto_schema(Args) 一致。"""
        from agent_runtime.schema_utils import auto_schema
        from agent_runtime.tools import GrepArgs
        from src.tools.ast_parser import AstParseArgs
        from src.tools.find_test import FindTestArgs
        from src.tools.git_tools import GitBlameArgs, GitDiffArgs
        from src.tools.java_ast_parser import JavaAstParseArgs
        from src.tools.java_stack_parser import JavaStackParseArgs
        from src.tools.stack_parser import StackParseArgs

        ctx = ToolContext(root=".")
        tools = build_repair_tools(ctx)

        args_map = {
            "grep": GrepArgs,
            "ast_parse": AstParseArgs,
            "stack_parse": StackParseArgs,
            "git_blame": GitBlameArgs,
            "git_diff": GitDiffArgs,
            "find_test": FindTestArgs,
            "java_ast_parse": JavaAstParseArgs,
            "java_stack_parse": JavaStackParseArgs,
        }

        for name, args_class in args_map.items():
            spec = tools.get(name)
            assert spec is not None, f"{name}: 未注册"
            expected = auto_schema(args_class)
            actual = spec["schema"]
            assert expected == actual, (
                f"{name}: schema 不一致\n  auto_schema: {expected}\n  registry: {actual}"
            )

    def test_description_mentions_required_params(self):
        """description 至少提及 schema 中的必填参数名。"""
        ctx = ToolContext(root=".")
        tools = build_repair_tools(ctx)
        for name, spec in tools.items():
            schema = spec.get("schema", {})
            required = [k for k, v in schema.items() if "=" not in str(v)]
            desc = spec.get("description", "")
            for param in required:
                assert param in desc, f"{name}: description 缺少参数 '{param}'"

    def test_no_null_run_functions(self):
        """所有工具的 run 不为 None。"""
        ctx = ToolContext(root=".")
        tools = build_repair_tools(ctx)
        for name, spec in tools.items():
            assert spec.get("run") is not None, f"{name}: run 为 None"

    def test_execution_tier_valid(self):
        ctx = ToolContext(root=".")
        tools = build_repair_tools(ctx)
        for name, spec in tools.items():
            tier = spec.get("execution_tier", "")
            assert tier in ("host", "container"), f"{name}: 无效 tier '{tier}'"
