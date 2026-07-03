"""Single-Agent Baseline：全量 Tool ReAct，与 Multi-Agent 共享 .repair() 接口。"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from agent_runtime.config import AgentConfig
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import build_tool_registry
from agent_runtime.workspace import WorkspaceContext

from src.middleware import ToolGateway
from src.orchestrator import Orchestrator
from src.repair_factory import create_model_client
from src.state import RepairState
from src.tools.registry import build_repair_tools
from src.tools.sandbox_tools import sandbox_build, sandbox_test, sandbox_verify

BASELINE_SYSTEM_PROMPT = (
    "你是代码修复专家。分析错误、定位代码、生成补丁、在容器内验证修复。你可以使用所有工具。"
)


def _build_baseline_tools(ctx: ToolContext) -> dict:
    tools = build_tool_registry(ctx)
    tools.update(build_repair_tools(ctx))
    tools.update(
        {
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
    )
    return tools


def _build_baseline_gateway(tool_names: list[str]) -> ToolGateway:
    table = {name: {"baseline"} for name in tool_names}
    table["*"] = {"baseline"}
    return ToolGateway(table)


def create_single_agent_baseline(model_client, workspace, cwd: str = "") -> Agent:
    """创建持有全部 Tool 的 Single-Agent Baseline。"""
    root = cwd or workspace.repo_root
    ctx = ToolContext(root=root)
    tools = _build_baseline_tools(ctx)

    agent = Agent(
        config=AgentConfig(
            provider="deepseek",
            max_steps=12,
            max_new_tokens=4096,
            approval="auto",
        ),
        model_client=model_client,
        workspace=workspace,
        cwd=root,
        tools=tools,
        system_prompt=BASELINE_SYSTEM_PROMPT,
    )

    gw = _build_baseline_gateway(list(tools.keys()))
    gw.wrap_agent("baseline", agent)
    return agent


class SingleAgentOrchestrator:
    """简化编排器：Issue → Single-Agent ask() → 解析并应用补丁。"""

    def __init__(self, agent: Agent, repo_root: str | None = None):
        self.agent = agent
        self._repo_root = repo_root or (
            agent._cwd
            or getattr(agent.workspace, "cwd", "")
            or agent.workspace.repo_root
            or str(Path.cwd())
        )

    def repair(
        self,
        issue: str,
        max_retries: int = 3,
        repair_timeout_s: int = 180,
    ) -> RepairState:
        state = RepairState(issue_input=issue, max_retries=max_retries)
        t0 = time.time()
        try:
            prompt = f"请修复以下 issue，可使用全部工具完成定位、修补与验证：\n\n{issue}"
            answer = self.agent.ask(prompt)
            helper = Orchestrator(None, None, None)
            helper._repo_root = self._repo_root
            patches = helper._parse_patches(answer)
            state.candidate_patches = patches
            if patches:
                applied = helper._apply_patches_on_disk(patches)
                if applied:
                    state.status = "patched"
                else:
                    state.status = "failed"
                    state.agent_errors["baseline"] = "patches parsed but not applied"
            else:
                state.status = "failed"
                state.agent_errors["baseline"] = "no patches in agent output"
        except Exception as exc:
            state.status = "failed"
            state.agent_errors["baseline"] = str(exc)
        state.node_timings["baseline_ms"] = int((time.time() - t0) * 1000)
        return state


def make_single_agent_factory(model_client=None) -> Callable[[str], SingleAgentOrchestrator]:
    """返回 `(repo_path) -> SingleAgentOrchestrator` 工厂。"""

    client = create_model_client(model_client)

    def factory(repo_path: str) -> SingleAgentOrchestrator:
        ws = WorkspaceContext.build(repo_path)
        repo = str(Path(repo_path).resolve())
        agent = create_single_agent_baseline(client, ws, cwd=repo)
        return SingleAgentOrchestrator(agent, repo)

    return factory
