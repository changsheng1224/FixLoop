"""L1 TaskState ↔ L2 RepairState 关联字段与 Orchestrator 绑定辅助。"""

from __future__ import annotations

from src.state import AgentAskRef

__all__ = [
    "AgentAskRef",
    "L2_BINDING_SCHEMA_VERSION",
    "bind_l2_context",
    "clear_l2_context",
    "make_repair_task_id",
]

L2_BINDING_SCHEMA_VERSION = 1


def make_repair_task_id(repair_run_id: str, agent: str, attempt: int = 0) -> str:
    """生成 repair 内唯一 task_id（retry 时带 attempt 后缀）。"""
    base = f"{repair_run_id}-{agent}"
    if attempt > 0:
        return f"{base}-{attempt}"
    return base


def bind_l2_context(
    agent,
    *,
    repair_run_id: str,
    agent_name: str,
    phase: str,
    attempt: int,
    started_ms: int = 0,
) -> str:
    """在 Agent.ask 前写入 L2 上下文，返回 task_id。"""
    task_id = make_repair_task_id(repair_run_id, agent_name, attempt)
    agent._l2_repair_run_id = repair_run_id
    agent._l2_agent = agent_name
    agent._l2_phase = phase
    agent._l2_attempt = int(attempt)
    agent._l2_task_id = task_id
    agent._l2_ask_started_ms = int(started_ms)
    return task_id


def clear_l2_context(agent) -> None:
    """ask 结束后清理 Agent 上的 L2 临时属性。"""
    if agent is None:
        return
    for attr in (
        "_l2_repair_run_id",
        "_l2_agent",
        "_l2_phase",
        "_l2_attempt",
        "_l2_task_id",
        "_l2_ask_started_ms",
    ):
        if hasattr(agent, attr):
            delattr(agent, attr)
