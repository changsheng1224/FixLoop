"""修复流水线 Agent 工厂：统一 create_* 构造逻辑。"""

from __future__ import annotations

from typing import Literal

from agent_runtime.config import AgentConfig
from agent_runtime.repair_budget import _DEFAULT_ALLOCATIONS
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from src.middleware import ToolGateway, build_repair_gateway
from src.prompts.loader import load_system_prompt
from src.tools.composite import build_repair_agent_tools

RepairAgentRole = Literal["patcher", "verifier"]

# 分 Agent 预算表 — prompt_budget 从 RepairBudgetContext 统一来源读取
_AGENT_DEFAULTS: dict[RepairAgentRole, dict] = {
    "patcher": {
        "max_steps": 10,
        "max_new_tokens": 4096,
        "prompt_budget": _DEFAULT_ALLOCATIONS["patcher"],
        "max_json_retries": 0,
    },
    "verifier": {
        "max_steps": 4,
        "max_new_tokens": 4096,
        "prompt_budget": _DEFAULT_ALLOCATIONS["verifier"],
    },
}


def create_repair_agent(
    role: RepairAgentRole,
    model_client,
    workspace,
    cwd: str = "",
    approval: str = "auto",
    *,
    dry_run: bool = False,
    l1_prefix=None,
    warm_context=None,
    budget=None,
    gateway: ToolGateway | None = None,
) -> Agent:
    """创建 Patcher 或 Verifier Agent。

    Args:
        approval: Gate 7 审批策略。headless repair 默认 ``auto``（Layer 1 Gateway
            承担角色隔离；Layer 2 仍执行并 trace ``approval_policy`` / ``gate_id``）。
            交互式 CLI 可传 ``ask`` 启用双层拦截。
    """
    root = cwd or workspace.repo_root
    ctx = ToolContext(root=root)
    tools = build_repair_agent_tools(ctx, role)

    defaults = _AGENT_DEFAULTS[role]
    gw = gateway or build_repair_gateway(root)
    system_prompt = load_system_prompt(role)
    agent_name = role
    gw.bind_tools(tools)

    json_mode = role == "verifier"
    if role == "patcher":
        system_prompt += (
            "\n\n【输出格式】优先 read_file → apply_patch（*** Begin/End Patch）修改文件；"
            "patch_file 仅作兜底；可用 quick_test 跑 FAIL_TO_PASS；"
            "完成后简短说明。仅当无法调用工具时才输出 CandidatePatch JSON 数组。"
        )
    elif json_mode:
        system_prompt += "\n\n【输出格式】只输出合法 JSON（不要包裹在 ```json 或 <final> 中）。"
    agent = Agent(
        config=AgentConfig(provider="deepseek", approval=approval, json_mode=json_mode, **defaults),
        model_client=model_client,
        workspace=workspace,
        cwd=root,
        tools=tools,
        system_prompt=system_prompt,
        agent_name=agent_name,
        tool_dispatch=gw.dispatch,
        prefix_mode="repair",
        dry_run=dry_run,
        l1_prefix=l1_prefix,
        warm_context=warm_context,
    )
    agent._repair_gateway = gw
    if budget is not None:
        agent._budget = budget
    return agent


def create_patcher(
    model_client, workspace, cwd: str = "", approval: str = "auto", **kwargs
) -> Agent:
    return create_repair_agent("patcher", model_client, workspace, cwd, approval=approval, **kwargs)


def create_verifier(
    model_client, workspace, cwd: str = "", approval: str = "auto", **kwargs
) -> Agent:
    return create_repair_agent(
        "verifier", model_client, workspace, cwd, approval=approval, **kwargs
    )


__all__ = [
    "RepairAgentRole",
    "create_patcher",
    "create_repair_agent",
    "create_verifier",
]
