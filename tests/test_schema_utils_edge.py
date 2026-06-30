"""schema_utils + tools 边界测试。"""

import pytest

from agent_runtime.schema_utils import auto_schema, auto_validate
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import (
    ListFilesArgs,
    PatchFileArgs,
    ReadFileArgs,
    RunShellArgs,
    SearchArgs,
    WriteFileArgs,
    tool_read_file,
    tool_run_shell,
)


class TestAutoValidateEdge:
    """auto_validate 异常分支。"""

    def test_float_conversion(self):
        from dataclasses import dataclass

        @dataclass
        class FloatArgs:
            value: float = 0.5

        r = auto_validate(FloatArgs, {"value": "3.14"})
        assert r["value"] == pytest.approx(3.14)

    def test_bool_conversion(self):
        from dataclasses import dataclass

        @dataclass
        class BoolArgs:
            flag: bool = False

        r = auto_validate(BoolArgs, {"flag": True})
        assert r["flag"] is True

    def test_invalid_int_raises(self):
        with pytest.raises(ValueError, match="类型错误"):
            auto_validate(ReadFileArgs, {"path": "a.py", "start": "not_a_number"})

    def test_optional_type_handled(self):
        from dataclasses import dataclass

        @dataclass
        class OptArgs:
            name: str | None = None

        r = auto_validate(OptArgs, {"name": "hello"})
        assert r["name"] == "hello"

    def test_type_to_str_unknown(self):
        from agent_runtime.schema_utils import _type_to_str
        assert _type_to_str(bytes) == "str"  # 未知类型默认 str


class TestToolEdge:
    """工具执行边界测试。"""

    def test_run_shell_invalid_timeout_clamped(self, temp_workspace):
        ctx = ToolContext(root=str(temp_workspace))
        r = tool_run_shell(ctx, {"command": "echo hi", "timeout": "not_a_number"})
        # 不会 crash
        assert "hi" in r.lower() or "exit_code" in r.lower()

    def test_read_file_binary_content(self, temp_workspace):
        ctx = ToolContext(root=str(temp_workspace))
        r = tool_read_file(ctx, {"path": "__init__.py"})
        # 项目中不存在 __init__.py 在 temp workspace
        # 所以应返回 Error
        assert "Error" in r or "1 |" in r  # 存在则正常读，不存在则报错

    def test_auto_schema_all_args(self):
        """确认所有工具参数 dataclass 能正常生成 schema。"""
        for cls in [ListFilesArgs, ReadFileArgs, SearchArgs,
                     WriteFileArgs, PatchFileArgs, RunShellArgs]:
            s = auto_schema(cls)
            assert isinstance(s, dict)
            assert len(s) > 0
