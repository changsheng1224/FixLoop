"""Verifier Agent：容器内验证执行者。

持有 sandbox_build + sandbox_test，其他 Agent 无权触发容器执行。
"""

from agent_runtime.config import AgentConfig
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from src.prompts.loader import load_system_prompt
from src.tools.sandbox_tools import build_sandbox_tool_registry


def create_verifier(model_client, workspace, cwd: str = "") -> Agent:
    root = cwd or workspace.repo_root
    ctx = ToolContext(root=root)

    tools = build_sandbox_tool_registry(ctx)

    system_prompt = load_system_prompt("verifier")

    agent = Agent(
        config=AgentConfig(provider="deepseek", max_steps=4, max_new_tokens=4096, approval="auto"),
        model_client=model_client,
        workspace=workspace,
        cwd=root,
        tools=tools,
        system_prompt=system_prompt,
    )

    from src.middleware import build_repair_gateway

    gw = build_repair_gateway()
    gw.grant("verifier", "sandbox_build")
    gw.grant("verifier", "sandbox_test")
    gw.grant("verifier", "sandbox_verify")
    gw.wrap_agent("verifier", agent)

    return agent
