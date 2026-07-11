"""Single-Agent Baseline：全量 Tool ReAct，与 Multi-Agent 共享 .repair() 接口。"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from agent_runtime.runtime import Agent
from agent_runtime.workspace import WorkspaceContext
from src.agents.factory import create_baseline_agent
from src.eval.token_usage import build_token_usage_summary, reset_client_session_usage
from src.repair.baseline_apply import apply_baseline_answer, snapshot_baseline_sources
from src.repair.termination import finalize_repair_state
from src.repair_factory import create_model_client
from src.state import RepairState


def create_single_agent_baseline(model_client, workspace, cwd: str = "") -> Agent:
    """创建持有全部 Tool 的 Single-Agent Baseline。"""
    return create_baseline_agent(model_client, workspace, cwd=cwd)


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
        before = snapshot_baseline_sources(repo)
        reset_client_session_usage(self.agent.model_client)
        repair_started_at = time.time()
        prompt = f"请修复以下 issue，可使用全部工具完成定位、修补与验证：\n\n{issue}"
        apply_baseline_answer(self.agent, self._repo_root, prompt, state, repo_before_apply=before)
        token_summary = build_token_usage_summary(
            self.agent.model_client,
            repo,
            since_ts=repair_started_at,
        )
        state.node_timings["total_tokens"] = token_summary["total_tokens"]
        state.node_timings["token_usage"] = token_summary
        state.node_timings["baseline_ms"] = int((time.time() - t0) * 1000)
        finalize_repair_state(state)
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
