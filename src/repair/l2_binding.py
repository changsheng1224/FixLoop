"""L1 TaskState ↔ L2 RepairState 关联字段与 Orchestrator 绑定辅助。"""

from __future__ import annotations

from dataclasses import asdict, dataclass

__all__ = [
    "AgentAskRef",
    "bind_l2_context",
    "clear_l2_context",
    "l2_payload_from_agent",
    "l2_payload_from_task_state",
    "make_repair_task_id",
]

L2_BINDING_SCHEMA_VERSION = 1


@dataclass
class AgentAskRef:
    """单次 L2 phase 内 Agent 调用（ask 或 synthetic complete_once）。"""

    agent: str
    phase: str
    attempt: int
    task_id: str
    run_id: str
    started_ms: int = 0
    finished_ms: int = 0
    stop_reason: str = ""
    tool_steps: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> AgentAskRef:
        return cls(
            agent=str(data.get("agent", "")),
            phase=str(data.get("phase", "")),
            attempt=int(data.get("attempt", 0) or 0),
            task_id=str(data.get("task_id", "")),
            run_id=str(data.get("run_id", "")),
            started_ms=int(data.get("started_ms", 0) or 0),
            finished_ms=int(data.get("finished_ms", 0) or 0),
            stop_reason=str(data.get("stop_reason", "")),
            tool_steps=int(data.get("tool_steps", 0) or 0),
        )


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


def l2_payload_from_agent(agent) -> dict:
    """从 Agent 临时 L2 属性构造 trace payload 片段。"""
    if agent is None or not getattr(agent, "_l2_agent", ""):
        return {}
    return {
        "task_id": getattr(agent, "_l2_task_id", ""),
        "repair_run_id": getattr(agent, "_l2_repair_run_id", ""),
        "l2_agent": getattr(agent, "_l2_agent", ""),
        "l2_phase": getattr(agent, "_l2_phase", ""),
        "l2_attempt": int(getattr(agent, "_l2_attempt", 0) or 0),
    }


def l2_payload_from_task_state(ts) -> dict:
    """从 TaskState L2 字段构造 trace payload 片段。"""
    if ts is None or not getattr(ts, "l2_agent", ""):
        return {}
    return {
        "task_id": ts.task_id,
        "repair_run_id": ts.l2_repair_run_id or ts.run_id,
        "l2_agent": ts.l2_agent,
        "l2_phase": ts.l2_phase,
        "l2_attempt": int(ts.l2_attempt or 0),
    }
