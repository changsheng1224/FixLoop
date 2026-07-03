"""Retriever Agent：代码搜索专家。"""

from agent_runtime.config import AgentConfig
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import build_tool_registry
from src.prompts.loader import load_system_prompt
from src.tools.registry import build_repair_tools


def create_retriever(model_client, workspace, cwd: str = "", light_client=None) -> Agent:
    root = cwd or workspace.repo_root
    ctx = ToolContext(root=root)

    tools = build_tool_registry(ctx)
    repair_tools = build_repair_tools(ctx)
    tools.update(
        {
            "git_blame": repair_tools["git_blame"],
            "git_diff": repair_tools["git_diff"],
            "find_test": repair_tools["find_test"],
        }
    )
    for banned in ("write_file", "patch_file", "run_shell", "ast_parse", "stack_parse"):
        tools.pop(banned, None)

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
