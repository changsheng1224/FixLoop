"""修复流水线 Agent 工厂：统一 create_* 构造逻辑。"""

from __future__ import annotations

import json

from agent_runtime.config import AgentConfig
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from src.middleware import ToolGateway, build_repair_gateway
from src.prompts.loader import load_system_prompt
from src.prompts.prefix import compose_repair_prefix
from src.tools.composite import build_repair_agent_tools


def load_repair_agent_config(role, root, *, approval=None, **overrides):
    from src.repair.policy import resolve_role_policy

    return resolve_role_policy(role, root, approval=approval, **overrides)


def create_repair_agent(
    role: str,
    model_client,
    workspace,
    cwd: str = "",
    approval: str | None = None,
    *,
    dry_run: bool = False,
    l1_prefix=None,
    warm_context=None,
    budget=None,
    gateway: ToolGateway | None = None,
    code_exploration_mode: str = "text",
    code_exploration_server_argv: tuple[str, ...] | None = None,
    sandbox_context: ToolContext | None = None,
    runtime_policy: AgentConfig | None = None,
) -> Agent:
    """创建 Patcher Agent。

    Args:
        approval: Gate 7 审批策略。headless repair 默认 ``auto``（Layer 1 Gateway
            承担角色隔离；Layer 2 仍执行并 trace ``approval_policy`` / ``gate_id``）。
            交互式 CLI 可传 ``ask`` 启用双层拦截。
    """
    if role != "patcher":
        raise ValueError("only the patcher model role can be constructed here")
    root = cwd or workspace.repo_root
    config = runtime_policy or load_repair_agent_config(
        role,
        root,
        approval=approval,
        code_exploration={
            "mode": code_exploration_mode,
            "server_argv": code_exploration_server_argv,
        },
    )
    approval = config.approval
    if (
        sandbox_context is not None
        and sandbox_context.sandbox_mode
        and (code_exploration_mode != "text" or code_exploration_server_argv is not None)
    ):
        raise ValueError("wsl_bwrap does not support LSP or external exploration services")
    if (
        sandbox_context is not None
        and sandbox_context.sandbox_mode
        and sandbox_context.exploration_service is not None
    ):
        raise ValueError("wsl_bwrap does not support external exploration services")
    if sandbox_context is not None and sandbox_context.root != root:
        raise ValueError("wsl_bwrap agent workspace mismatch")
    ctx = sandbox_context or ToolContext(
        root=root,
        exploration_mode=code_exploration_mode,
        lsp_argv=code_exploration_server_argv,
    )
    ctx.web_config = config.web
    tools = build_repair_agent_tools(ctx, role)
    delegation_tools = {}
    if role == "patcher":
        from src.collaboration.delegation import build_delegation_tools
        from src.prompts.prefix import build_repair_l1_prefix

        if l1_prefix is None:
            l1_prefix = build_repair_l1_prefix(
                workspace, tools, dry_run=dry_run, approval=approval, repo_root=root
            )
        delegation_tools = build_delegation_tools(ctx)
        tools.update(delegation_tools)

    gw = gateway or build_repair_gateway(root, sandbox_mode=ctx.sandbox_backend is not None)
    system_prompt = load_system_prompt(role)
    if config.web.enabled:
        system_prompt += "\n\n" + load_system_prompt("patcher_web")
    agent_name = role
    gw.bind_tools(tools)

    if role == "patcher":
        system_prompt += (
            "\n\n【输出格式】优先 read_file → apply_patch（*** Begin/End Patch）修改文件；"
            "patch_file 仅作兜底；可用 quick_test 运行与问题相关的仓库测试；"
            "完成后简短说明。无法调用工具时报告阻塞原因。"
        )
        # Owner control tools belong to the L2 role projection, preserving the
        # canonical L1 prefix remains independent of the role projection.
        system_prompt += "\n\nOwner exploration tools: " + json.dumps(
            {
                name: {"description": spec["description"], "schema": spec["schema"]}
                for name, spec in delegation_tools.items()
            },
            ensure_ascii=False,
        )

    def dispatch(agent_name, name, execute):
        runtime = getattr(ctx, "readonly_exploration_runtime", None)
        if runtime is not None and tools.get(name, {}).get("side_effect") in {
            "write",
            "process",
            "verify",
        }:
            runtime.before_write()
        return gw.dispatch(agent_name, name, execute)

    from src.repair.loop_policy import RepairLoopPolicy

    agent = Agent(
        config=config,
        model_client=model_client,
        workspace=workspace,
        cwd=root,
        tools=tools,
        system_prompt=system_prompt,
        agent_name=agent_name,
        tool_dispatch=dispatch,
        prefix_builder=lambda text, agent: compose_repair_prefix(l1_prefix, text),
        context_budget=budget,
        tool_gateway=gw,
        dry_run=dry_run,
        warm_context=warm_context,
        tool_context=ctx,
        loop_policy=RepairLoopPolicy() if role == "patcher" else None,
        context_allowed_kinds=(
            "memory",
            "observation",
            "source",
            "patch",
            "verification",
            "history",
        ),
    )
    return agent


def create_patcher(
    model_client, workspace, cwd: str = "", approval: str | None = None, **kwargs
) -> Agent:
    return create_repair_agent("patcher", model_client, workspace, cwd, approval=approval, **kwargs)


__all__ = ["create_patcher", "load_repair_agent_config"]
