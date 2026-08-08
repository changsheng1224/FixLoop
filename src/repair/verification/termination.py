"""Repair 流水线终态 status 枚举与解析。"""

from __future__ import annotations

from src.state import RepairState, RepairStatus

__all__ = [
    "RepairTerminalStatus",
    "TERMINAL_STATUSES",
    "apply_terminal_status",
    "finalize_repair_state",
    "has_repair_timeout",
    "introduced_regression",
    "is_repair_success",
    "is_terminal",
    "mark_fixed_skip_verify",
    "regression_detected",
]


RepairTerminalStatus = RepairStatus
TERMINAL_STATUSES = frozenset(
    status.value
    for status in RepairStatus
    if status is not RepairStatus.PENDING
)


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES or status == "patched"


def is_repair_success(state: RepairState) -> bool:
    """修复是否算成功（fixed，或 legacy patched 且有补丁）。"""
    if state.status == RepairTerminalStatus.FIXED and state.candidate_patches:
        return True
    return state.status == "patched" and bool(state.candidate_patches)


def mark_fixed_skip_verify(state: RepairState) -> None:
    """标记修复成功且跳过验证（--skip-verify / 无 Verifier）。"""
    state.set_status(RepairTerminalStatus.FIXED, "verify_skipped")
    state.node_timings["verify_skipped"] = True


def has_repair_timeout(state: RepairState) -> bool:
    if state.status == RepairTerminalStatus.TIMEOUT:
        return True
    if state.node_timings.get("repair_timeout"):
        return True
    if state.node_timings.get("phase_timeout"):
        return True
    orch_err = state.agent_errors.get("orchestrator", "")
    lowered = orch_err.lower()
    return "repair timeout" in lowered or "phase timeout" in lowered


def regression_detected(pre_code: int | None, post_code: int | None) -> bool:
    """pytest 退出码语义：baseline 全绿后 patch 引入新失败。"""
    if pre_code is None or post_code is None:
        return False
    return pre_code == 0 and post_code != 0


def introduced_regression(state: RepairState) -> bool:
    if state.node_timings.get("introduced_regression"):
        return True
    pre = state.node_timings.get("baseline_pytest_code")
    post = state.node_timings.get("post_patch_pytest_code")
    return regression_detected(pre, post)


def apply_terminal_status(state: RepairState) -> None:
    """将 RepairState.status 规范为终态枚举值。"""
    if state.node_timings.get("user_cancel"):
        state.set_status(RepairTerminalStatus.USER_CANCEL, "user_cancel")
        return
    if has_repair_timeout(state):
        state.set_status(RepairTerminalStatus.TIMEOUT, "repair_timeout")
        return
    if state.status == RepairTerminalStatus.FIXED:
        return
    if introduced_regression(state):
        state.node_timings["introduced_regression"] = True
        state.set_status(RepairTerminalStatus.REGRESSION, "introduced_regression")
        return
    from src.repair.stop_loss import has_stop_loss

    if has_stop_loss(state) or state.retry_count >= state.max_retries:
        state.set_status(RepairTerminalStatus.EXHAUSTED, "retry_or_stop_loss_exhausted")
        return
    if state.status in TERMINAL_STATUSES:
        return
    state.set_status(RepairTerminalStatus.FAILED, "repair_failed")


def finalize_repair_state(state: RepairState) -> None:
    """统一收尾：终态 status + failure_tags。"""
    from src.repair.failure_tags import apply_failure_tags

    apply_terminal_status(state)
    apply_failure_tags(state)
