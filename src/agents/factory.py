"""修复流水线 Agent 工厂：统一 create_* 构造逻辑。"""

from __future__ import annotations

from typing import Literal

from agent_runtime.config import AgentConfig
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from src.middleware import ToolGateway, build_repair_gateway
from src.prompts.loader import load_system_prompt
from src.tools.composite import build_repair_agent_tools

MultiAgentRole = Literal["localizer", "retriever", "patcher", "verifier"]
RepairAgentRole = Literal["localizer", "retriever", "patcher", "verifier", "baseline"]

BASELINE_SYSTEM_PROMPT = (
    "你是代码修复专家。分析错误、定位代码、生成补丁、在容器内验证修复。你可以使用所有工具。"
)

_AGENT_DEFAULTS: dict[MultiAgentRole, dict] = {
    "localizer": {"max_steps": 6, "max_new_tokens": 4096},
    "retriever": {"max_steps": 4, "max_new_tokens": 2048},
    "patcher": {"max_steps": 6, "max_new_tokens": 4096},
    "verifier": {"max_steps": 4, "max_new_tokens": 4096},
}


def _baseline_gateway(tool_names: list[str]) -> ToolGateway:
    table = {name: {"baseline"} for name in tool_names}
    table["*"] = {"baseline"}
    return ToolGateway(table)


def create_repair_agent(
    role: RepairAgentRole,
    model_client,
    workspace,
    cwd: str = "",
    light_client=None,
    approval: str = "auto",
    *,
    dry_run: bool = False,
    l1_prefix=None,
) -> Agent:
    """创建指定角色的修复 Agent（含 baseline 单 Agent 变体）。

    Args:
        approval: Gate 7 审批策略。headless repair 默认 ``auto``（Layer 1 Gateway
            承担角色隔离；Layer 2 仍执行并 trace ``approval_policy`` / ``gate_id``）。
            交互式 CLI 可传 ``ask`` 启用双层拦截。
    """
    root = cwd or workspace.repo_root
    ctx = ToolContext(root=root)
    tools = build_repair_agent_tools(ctx, role)

    if role == "baseline":
        defaults = {"max_steps": 12, "max_new_tokens": 4096}
        gw = _baseline_gateway(list(tools.keys()))
        system_prompt = BASELINE_SYSTEM_PROMPT
        agent_name = "baseline"
        light = None
    else:
        defaults = _AGENT_DEFAULTS[role]
        gw = build_repair_gateway()
        if role == "verifier":
            gw.grant("verifier", "sandbox_build")
            gw.grant("verifier", "sandbox_test")
            gw.grant("verifier", "sandbox_verify")
        system_prompt = load_system_prompt(role)
        agent_name = role
        light = light_client if role in ("localizer", "retriever") else None

    json_mode = role in ("localizer", "patcher", "retriever")
    if json_mode:
        system_prompt += "\n\n【输出格式】只输出合法 JSON（不要包裹在 ```json 或 <final> 中）。"
    return Agent(
        config=AgentConfig(provider="deepseek", approval=approval, json_mode=json_mode, **defaults),
        model_client=model_client,
        workspace=workspace,
        cwd=root,
        tools=tools,
        system_prompt=system_prompt,
        light_client=light,
        agent_name=agent_name,
        tool_dispatch=gw.dispatch,
        prefix_mode="repair",
        dry_run=dry_run,
        l1_prefix=l1_prefix,
    )


def create_localizer(
    model_client, workspace, cwd: str = "", light_client=None, approval: str = "auto", **kwargs
) -> Agent:
    return create_repair_agent(
        "localizer", model_client, workspace, cwd, light_client, approval=approval, **kwargs
    )


def create_patcher(model_client, workspace, cwd: str = "", approval: str = "auto", **kwargs) -> Agent:
    return create_repair_agent("patcher", model_client, workspace, cwd, approval=approval, **kwargs)


def create_retriever(
    model_client, workspace, cwd: str = "", light_client=None, approval: str = "auto", **kwargs
) -> Agent:
    return create_repair_agent(
        "retriever", model_client, workspace, cwd, light_client, approval=approval, **kwargs
    )


def create_verifier(model_client, workspace, cwd: str = "", approval: str = "auto", **kwargs) -> Agent:
    return create_repair_agent("verifier", model_client, workspace, cwd, approval=approval, **kwargs)


def create_baseline_agent(model_client, workspace, cwd: str = "", approval: str = "auto", **kwargs) -> Agent:
    return create_repair_agent("baseline", model_client, workspace, cwd, approval=approval, **kwargs)


__all__ = [
    "BASELINE_SYSTEM_PROMPT",
    "MultiAgentRole",
    "RepairAgentRole",
    "create_baseline_agent",
    "create_localizer",
    "create_patcher",
    "create_repair_agent",
    "create_retriever",
    "create_verifier",
]
