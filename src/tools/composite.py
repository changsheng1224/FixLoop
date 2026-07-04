"""修复 Agent 工具注册表组合：按 role 合并 L1 基础工具与 L2 域工具。"""

from __future__ import annotations

from typing import Literal

from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import build_tool_registry
from src.tools.registry import build_repair_tools
from src.tools.sandbox_tools import build_sandbox_tool_registry

RepairAgentRole = Literal["localizer", "retriever", "patcher", "verifier", "baseline"]

_WRITE_TOOLS = ("write_file", "patch_file", "run_shell")
_LOCALIZER_REMOVE = _WRITE_TOOLS
_RETRIEVER_REMOVE = _WRITE_TOOLS + ("ast_parse", "stack_parse")


def build_repair_agent_tools(ctx: ToolContext, role: RepairAgentRole) -> dict:
    """按修复流水线角色返回完整工具注册表。"""
    if role == "verifier":
        return build_sandbox_tool_registry(ctx)

    if role == "baseline":
        tools = build_tool_registry(ctx)
        tools.update(build_repair_tools(ctx))
        tools.update(build_sandbox_tool_registry(ctx))
        return tools

    tools = build_tool_registry(ctx)
    repair_tools = build_repair_tools(ctx)

    if role == "localizer":
        tools.update(
            {
                "ast_parse": repair_tools["ast_parse"],
                "stack_parse": repair_tools["stack_parse"],
            }
        )
        for name in _LOCALIZER_REMOVE:
            tools.pop(name, None)
    elif role == "retriever":
        tools.update(
            {
                "git_blame": repair_tools["git_blame"],
                "git_diff": repair_tools["git_diff"],
                "find_test": repair_tools["find_test"],
            }
        )
        for name in _RETRIEVER_REMOVE:
            tools.pop(name, None)
    elif role == "patcher":
        tools.pop("run_shell", None)

    return tools
