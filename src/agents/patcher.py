"""Patcher Agent：补丁生成者。"""

from agent_runtime.config import AgentConfig
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from src.prompts.loader import load_system_prompt
from src.tools.composite import build_repair_agent_tools


def create_patcher(model_client, workspace, cwd: str = "") -> Agent:
    """创建 Patcher Agent（read/search + write/patch）。"""
    root = cwd or workspace.repo_root
    ctx = ToolContext(root=root)
    tools = build_repair_agent_tools(ctx, "patcher")
    system_prompt = load_system_prompt("patcher")

    agent = Agent(
        config=AgentConfig(provider="deepseek", max_steps=6, max_new_tokens=4096, approval="auto"),
        model_client=model_client,
        workspace=workspace,
        cwd=root,
        tools=tools,
        system_prompt=system_prompt,
    )

    from src.middleware import build_repair_gateway

    build_repair_gateway().wrap_agent("patcher", agent)
    return agent
