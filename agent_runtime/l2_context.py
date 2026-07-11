"""L1 侧 L2 上下文 trace payload（不依赖 src.repair）。"""

from __future__ import annotations

__all__ = [
    "l2_payload_from_agent",
    "l2_payload_from_task_state",
]


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
