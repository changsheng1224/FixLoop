"""修复 Agent 工具注册表组合：按 role 合并 L1 基础工具与 L2 域工具。"""

from __future__ import annotations

from typing import Literal

from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import build_tool_registry
from src.tools.registry import build_repair_tools
from src.tools.sandbox_tools import build_sandbox_tool_registry

RepairAgentRole = Literal["localizer", "retriever", "patcher", "verifier", "baseline"]

REPAIR_CANONICAL_TOOL_NAMES: tuple[str, ...] = (
    "ast_parse",
    "find_test",
    "git_blame",
    "git_diff",
    "list_files",
    "patch_file",
    "read_file",
    "run_shell",
    "sandbox_build",
    "sandbox_test",
    "sandbox_verify",
    "search",
    "stack_parse",
    "write_file",
)


def build_repair_canonical_tools(ctx: ToolContext) -> dict:
    """Repair 流水线 canonical 工具全集（字典序固定，各 phase 共用）。"""
    tools = build_tool_registry(ctx)
    tools.update(build_repair_tools(ctx))
    tools.update(build_sandbox_tool_registry(ctx))
    return {name: tools[name] for name in REPAIR_CANONICAL_TOOL_NAMES}


def is_repair_canonical_registry(tools: dict) -> bool:
    """注册表是否为 repair canonical 全集。"""
    return tuple(sorted(tools.keys())) == REPAIR_CANONICAL_TOOL_NAMES


def build_repair_agent_tools(ctx: ToolContext, role: RepairAgentRole) -> dict:
    """按修复流水线角色返回 canonical 工具注册表（执行权限由 ToolGateway 控制）。"""
    del role  # 各 phase 同一 schema 集；权限见 src/middleware.py
    return build_repair_canonical_tools(ctx)
