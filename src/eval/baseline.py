"""Single-Agent Baseline：全量 Tool ReAct，与 Multi-Agent 共享 .repair() 接口。"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from agent_runtime.config import AgentConfig
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from agent_runtime.workspace import WorkspaceContext
from src.eval.runner import should_include_in_eval_diff
from src.eval.token_usage import build_token_usage_summary, reset_client_session_usage
from src.middleware import ToolGateway
from src.repair.patch_applier import PatchApplier, parse_patches
from src.repair_factory import create_model_client
from src.state import RepairState
from src.tools.composite import build_repair_agent_tools

BASELINE_SYSTEM_PROMPT = (
    "你是代码修复专家。分析错误、定位代码、生成补丁、在容器内验证修复。你可以使用所有工具。"
)


def _build_baseline_tools(ctx: ToolContext) -> dict:
    return build_repair_agent_tools(ctx, "baseline")


def _build_baseline_gateway(tool_names: list[str]) -> ToolGateway:
    table = {name: {"baseline"} for name in tool_names}
    table["*"] = {"baseline"}
    return ToolGateway(table)


def create_single_agent_baseline(model_client, workspace, cwd: str = "") -> Agent:
    """创建持有全部 Tool 的 Single-Agent Baseline。"""
    root = cwd or workspace.repo_root
    ctx = ToolContext(root=root)
    tools = _build_baseline_tools(ctx)

    gw = _build_baseline_gateway(list(tools.keys()))
    return Agent(
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
        agent_name="baseline",
        tool_policy=gw.can_call,
    )


def _snapshot_source_tree(repo: Path) -> dict[str, str]:
    """读取 repo 内参与评测 diff 的源码快照。"""
    snapshot: dict[str, str] = {}
    if not repo.is_dir():
        return snapshot
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(repo).as_posix()
        if should_include_in_eval_diff(rel):
            snapshot[rel] = path.read_text(encoding="utf-8")
    return snapshot


def _repo_sources_changed(repo: Path, before: dict[str, str]) -> bool:
    return _snapshot_source_tree(repo) != before


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
        """Single-Agent ReAct 修复：ask → 解析补丁 → 写盘 → 记录 token 用量。"""
        state = RepairState(issue_input=issue, max_retries=max_retries)
        t0 = time.time()
        repo = Path(self._repo_root)
        before = _snapshot_source_tree(repo)
        reset_client_session_usage(self.agent.model_client)
        repair_started_at = time.time()
        try:
            prompt = f"请修复以下 issue，可使用全部工具完成定位、修补与验证：\n\n{issue}"
            answer = self.agent.ask(prompt)
            applier = PatchApplier(self._repo_root)
            patches = parse_patches(answer)
            state.candidate_patches = patches
            if patches:
                applied = applier.apply_patches(patches)
                if applied:
                    state.status = "patched"
                elif _repo_sources_changed(repo, before):
                    state.status = "patched"
                else:
                    state.status = "failed"
                    state.agent_errors["baseline"] = "patches parsed but not applied"
            elif _repo_sources_changed(repo, before):
                state.status = "patched"
            else:
                state.status = "failed"
                state.agent_errors["baseline"] = "no patches in agent output"
        except Exception as exc:
            state.status = "failed"
            state.agent_errors["baseline"] = str(exc)
        token_summary = build_token_usage_summary(
            self.agent.model_client,
            repo,
            since_ts=repair_started_at,
        )
        state.node_timings["total_tokens"] = token_summary["total_tokens"]
        state.node_timings["token_usage"] = token_summary
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
