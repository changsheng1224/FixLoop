"""Localizer Agent：代码定位专家。"""

from pathlib import Path

from agent_runtime.config import AgentConfig
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import build_tool_registry
from src.tools.registry import build_repair_tools


def create_localizer(model_client, workspace, cwd: str = "", light_client=None) -> Agent:
    root = cwd or workspace.repo_root
    ctx = ToolContext(root=root)

    tools = build_tool_registry(ctx)
    repair_tools = build_repair_tools(ctx)
    tools.update(
        {
            "ast_parse": repair_tools["ast_parse"],
            "stack_parse": repair_tools["stack_parse"],
        }
    )
    for banned in ("write_file", "patch_file", "run_shell"):
        tools.pop(banned, None)

    prompt_file = Path(__file__).parent.parent / "prompts" / "localizer.txt"
    system_prompt = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else ""

    agent = Agent(
        config=AgentConfig(provider="deepseek", max_steps=6, max_new_tokens=4096, approval="auto"),
        model_client=model_client,
        workspace=workspace,
        cwd=root,
        tools=tools,
        system_prompt=system_prompt,
        light_client=light_client,
    )

    from src.middleware import build_repair_gateway

    build_repair_gateway().wrap_agent("localizer", agent)
    return agent
