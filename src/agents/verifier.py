"""Verifier Agent：容器内验证执行者。

持有 sandbox_build + sandbox_test，其他 Agent 无权触发容器执行。
"""

from pathlib import Path

from agent_runtime.config import AgentConfig
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from src.tools.sandbox_tools import (
    sandbox_build,
    sandbox_test,
    sandbox_verify,
)


def create_verifier(model_client, workspace, cwd: str = "") -> Agent:
    root = cwd or workspace.repo_root
    ctx = ToolContext(root=root)

    tools = {
        "sandbox_build": {
            "schema": {"repo_path": "str"},
            "risky": False,
            "description": "在 Docker 容器内执行 pip install。参数: repo_path",
            "run": lambda args: sandbox_build(ctx, args),
        },
        "sandbox_test": {
            "schema": {"repo_path": "str", "test_path": "str="},
            "risky": False,
            "description": "在 Docker 容器内运行 pytest。参数: repo_path, test_path",
            "run": lambda args: sandbox_test(ctx, args),
        },
        "sandbox_verify": {
            "schema": {"repo_path": "str", "test_path": "str="},
            "risky": False,
            "description": "单容器 build+test。参数: repo_path, test_path",
            "run": lambda args: sandbox_verify(ctx, args),
        },
    }

    prompt_file = Path(__file__).parent.parent / "prompts" / "verifier.txt"
    system_prompt = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else ""

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
