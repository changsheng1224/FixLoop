"""Repair 流水线终态 status 枚举与解析。"""

from __future__ import annotations

from enum import StrEnum

from src.state import RepairState

__all__ = [
    "RepairTerminalStatus",
    "TERMINAL_STATUSES",
    "apply_terminal_status",
    "introduced_regression",
    "is_repair_success",
    "is_terminal",
]


class RepairTerminalStatus(StrEnum):
    FIXED = "fixed"
    EXHAUSTED = "exhausted"
    REGRESSION = "regression"
    TIMEOUT = "timeout"
    USER_CANCEL = "user_cancel"
    FAILED = "failed"


TERMINAL_STATUSES = frozenset(s.value for s in RepairTerminalStatus)

# 进行中（非终态）
_IN_PROGRESS = frozenset({"pending", "localizing", "retrieving", "patching", "verifying"})


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES or status == "patched"


def is_repair_success(state: RepairState) -> bool:
    """修复是否算成功（fixed，或 legacy patched 且有补丁）。"""
    if state.status == RepairTerminalStatus.FIXED and state.candidate_patches:
        return True
    return state.status == "patched" and bool(state.candidate_patches)


def _has_timeout(state: RepairState) -> bool:
    if state.node_timings.get("repair_timeout"):
        return True
    orch_err = state.agent_errors.get("orchestrator", "")
    return "repair timeout" in orch_err


def introduced_regression(state: RepairState) -> bool:
    if state.node_timings.get("introduced_regression"):
        return True
    pre = state.node_timings.get("baseline_pytest_code")
    post = state.node_timings.get("post_patch_pytest_code")
    if pre is None or post is None:
        return False
    return pre == 0 and post != 0


def apply_terminal_status(state: RepairState) -> None:
    """将 RepairState.status 规范为终态枚举值。"""
    if state.node_timings.get("user_cancel"):
        state.status = RepairTerminalStatus.USER_CANCEL
        return
    if _has_timeout(state):
        state.status = RepairTerminalStatus.TIMEOUT
        return
    if state.status == RepairTerminalStatus.FIXED:
        return
    if introduced_regression(state):
        state.node_timings["introduced_regression"] = True
        state.status = RepairTerminalStatus.REGRESSION
        return
    if state.retry_count >= state.max_retries:
        state.status = RepairTerminalStatus.EXHAUSTED
        return
    if state.status in TERMINAL_STATUSES:
        return
    state.status = RepairTerminalStatus.FAILED
