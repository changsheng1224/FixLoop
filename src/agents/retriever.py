"""Retriever Agent：代码搜索专家。"""

from agent_runtime.config import AgentConfig
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from src.prompts.loader import load_system_prompt
from src.tools.composite import build_repair_agent_tools


def create_retriever(model_client, workspace, cwd: str = "", light_client=None) -> Agent:
    """创建 Retriever Agent（git/find_test 检索，无写权限）。"""
    root = cwd or workspace.repo_root
    ctx = ToolContext(root=root)
    tools = build_repair_agent_tools(ctx, "retriever")
    system_prompt = load_system_prompt("retriever")

    agent = Agent(
        config=AgentConfig(provider="deepseek", max_steps=4, max_new_tokens=2048, approval="auto"),
        model_client=model_client,
        workspace=workspace,
        cwd=root,
        tools=tools,
        system_prompt=system_prompt,
        light_client=light_client,
    )

    from src.middleware import build_repair_gateway

    build_repair_gateway().wrap_agent("retriever", agent)
    return agent
