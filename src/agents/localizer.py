"""Localizer Agent：代码定位专家。"""

from pathlib import Path

from agent_runtime.config import AgentConfig
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import build_tool_registry
from src.tools.registry import build_repair_tools


def create_localizer(model_client, workspace, cwd: str = "") -> Agent:
    """创建 Localizer Agent 实例。

    持有工具：ast_parse / stack_parse / read_file / search / git_blame。
    """
    root = cwd or workspace.repo_root
    ctx = ToolContext(root=root)

    # Layer 1 基础工具 + Layer 2 修复工具
    tools = build_tool_registry(ctx)
    repair_tools = build_repair_tools(ctx)
    # 只保留 Localizer 专用的修复工具
    localizer_tools = {
        "ast_parse": repair_tools["ast_parse"],
        "stack_parse": repair_tools["stack_parse"],
    }
    tools.update(localizer_tools)

    # 移除不属于 Localizer 的工具
    for banned in ("write_file", "patch_file", "run_shell"):
        tools.pop(banned, None)

    config = AgentConfig(
        provider="deepseek", max_steps=6, max_new_tokens=1024, approval="auto",
    )

    prompt_file = Path(__file__).parent.parent / "prompts" / "localizer.txt"
    system_prompt = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else ""

    agent = Agent(config=config, model_client=model_client, workspace=workspace, cwd=root)
    # 替换工具和 prefix
    agent.tools = tools
    agent._tool_names = set(tools.keys())
    if system_prompt:
        agent._prefix.text = system_prompt + "\n\n" + agent.workspace.text()
    return agent
