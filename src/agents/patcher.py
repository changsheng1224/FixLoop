"""Patcher Agent：补丁生成者。"""

from pathlib import Path

from agent_runtime.config import AgentConfig
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import build_tool_registry


def create_patcher(model_client, workspace, cwd: str = "") -> Agent:
    root = cwd or workspace.repo_root
    ctx = ToolContext(root=root)

    tools = build_tool_registry(ctx)
    tools.pop("run_shell", None)

    prompt_file = Path(__file__).parent.parent / "prompts" / "patcher.txt"
    system_prompt = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else ""

    agent = Agent(
        config=AgentConfig(provider="deepseek", max_steps=6, max_new_tokens=4096, approval="auto"),
        model_client=model_client, workspace=workspace, cwd=root,
        tools=tools, system_prompt=system_prompt,
    )

    from src.middleware import build_repair_gateway
    build_repair_gateway().wrap_agent("patcher", agent)
    return agent
