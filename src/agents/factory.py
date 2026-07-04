"""修复流水线 Agent 工厂：统一 create_* 构造逻辑。"""

from __future__ import annotations

from typing import Literal

from agent_runtime.config import AgentConfig
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from src.middleware import build_repair_gateway
from src.prompts.loader import load_system_prompt
from src.tools.composite import build_repair_agent_tools

MultiAgentRole = Literal["localizer", "retriever", "patcher", "verifier"]

_AGENT_DEFAULTS: dict[MultiAgentRole, dict] = {
    "localizer": {"max_steps": 6, "max_new_tokens": 4096},
    "retriever": {"max_steps": 4, "max_new_tokens": 2048},
    "patcher": {"max_steps": 6, "max_new_tokens": 4096},
    "verifier": {"max_steps": 4, "max_new_tokens": 4096},
}


def create_repair_agent(
    role: MultiAgentRole,
    model_client,
    workspace,
    cwd: str = "",
    light_client=None,
) -> Agent:
    """创建指定角色的修复 Agent。"""
    root = cwd or workspace.repo_root
    ctx = ToolContext(root=root)
    tools = build_repair_agent_tools(ctx, role)
    defaults = _AGENT_DEFAULTS[role]

    gw = build_repair_gateway()
    if role == "verifier":
        gw.grant("verifier", "sandbox_build")
        gw.grant("verifier", "sandbox_test")
        gw.grant("verifier", "sandbox_verify")

    return Agent(
        config=AgentConfig(provider="deepseek", approval="auto", **defaults),
        model_client=model_client,
        workspace=workspace,
        cwd=root,
        tools=tools,
        system_prompt=load_system_prompt(role),
        light_client=light_client if role in ("localizer", "retriever") else None,
        agent_name=role,
        tool_policy=gw.can_call,
    )


def create_localizer(model_client, workspace, cwd: str = "", light_client=None) -> Agent:
    return create_repair_agent("localizer", model_client, workspace, cwd, light_client)


def create_patcher(model_client, workspace, cwd: str = "") -> Agent:
    return create_repair_agent("patcher", model_client, workspace, cwd)


def create_retriever(model_client, workspace, cwd: str = "", light_client=None) -> Agent:
    return create_repair_agent("retriever", model_client, workspace, cwd, light_client)


def create_verifier(model_client, workspace, cwd: str = "") -> Agent:
    return create_repair_agent("verifier", model_client, workspace, cwd)


__all__ = [
    "create_localizer",
    "create_patcher",
    "create_repair_agent",
    "create_retriever",
    "create_verifier",
]
