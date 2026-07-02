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

    # Patcher 只有写工具+只读工具，没有 AST/Stack
    for banned in ("run_shell",):
        tools.pop(banned, None)

    config = AgentConfig(
        provider="deepseek", max_steps=6, max_new_tokens=1024, approval="auto",
    )

    prompt_file = Path(__file__).parent.parent / "prompts" / "patcher.txt"
    system_prompt = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else ""

    agent = Agent(config=config, model_client=model_client, workspace=workspace, cwd=root)
    agent.tools = tools
    agent._tool_names = set(tools.keys())
    if system_prompt:
        agent._prefix.text = system_prompt + "\n\n" + agent.workspace.text()
    return agent
