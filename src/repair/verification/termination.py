"""Repair 流水线终态 status 枚举与解析。"""

from __future__ import annotations

from src.state import RepairState, RepairStatus

__all__ = [
    "RepairTerminalStatus",
    "TERMINAL_STATUSES",
    "apply_terminal_status",
    "finalize_repair_state",
    "has_actionable_patch",
    "has_repair_timeout",
    "introduced_regression",
    "is_repair_success",
    "is_terminal",
    "mark_pending_verify",
    "regression_detected",
    "resolve_terminal_status",
]


RepairTerminalStatus = RepairStatus
TERMINAL_STATUSES = frozenset(
    status.value for status in RepairStatus if status is not RepairStatus.PENDING
)


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def is_repair_success(state: RepairState) -> bool:
    """修复是否算成功（fixed 且有补丁）。"""
    return resolve_terminal_status(state) == RepairTerminalStatus.FIXED and bool(
        state.candidate_patches
    )


def has_actionable_patch(state: RepairState) -> bool:
    """补丁可交付给后续验证；不等同于已经验证成功。"""
    return bool(state.candidate_patches) and resolve_terminal_status(state) in {
        RepairTerminalStatus.FIXED,
        RepairTerminalStatus.PENDING_VERIFY,
    }


def mark_pending_verify(state: RepairState) -> None:
    """记录已生成补丁但尚未验证，不把它计作修复成功。"""
    state.set_status(RepairTerminalStatus.PENDING_VERIFY, "verify_skipped")
    state.control.verify_skipped = True


def has_repair_timeout(state: RepairState) -> bool:
    if _control_terminal_status(state) is not None:
        return False
    if state.status == RepairTerminalStatus.TIMEOUT:
        return True
    if state.control.repair_timeout:
        return True
    if state.control.phase_timeout:
        return True
    return False


def regression_detected(pre_code: int | None, post_code: int | None) -> bool:
    """pytest 退出码语义：baseline 全绿后 patch 引入新失败。"""
    if pre_code is None or post_code is None:
        return False
    return pre_code == 0 and post_code != 0


def introduced_regression(state: RepairState) -> bool:
    if state.control.introduced_regression:
        return True
    pre = state.control.baseline_pytest_code
    post = state.control.post_patch_pytest_code
    return regression_detected(pre, post)


def _control_terminal_status(state: RepairState) -> RepairStatus | None:
    if (
        state.status == RepairStatus.RECOVERY_REQUIRED
        or state.control.coordination_status == "recovery_required"
    ):
        return RepairStatus.RECOVERY_REQUIRED
    if state.status == RepairStatus.USER_CANCEL or state.control.user_cancel:
        return RepairStatus.USER_CANCEL
    return None


def resolve_terminal_status(state: RepairState) -> RepairStatus:
    """Pure terminal resolver shared by finalization, CLI and reports."""
    control_status = _control_terminal_status(state)
    if control_status is not None:
        return control_status
    if has_repair_timeout(state):
        return RepairTerminalStatus.TIMEOUT
    if state.status == RepairTerminalStatus.FIXED:
        return RepairTerminalStatus.FIXED
    if introduced_regression(state):
        return RepairTerminalStatus.REGRESSION
    from src.repair.stop_loss import has_stop_loss

    if has_stop_loss(state) or state.retry_count >= state.max_retries:
        return RepairTerminalStatus.EXHAUSTED
    if state.status in TERMINAL_STATUSES:
        return RepairStatus(state.status)
    return RepairTerminalStatus.FAILED


def apply_terminal_status(state: RepairState) -> None:
    status = resolve_terminal_status(state)
    if status == RepairStatus.REGRESSION:
        state.control.introduced_regression = True
    state.set_status(status, f"terminal:{status.value}")


def finalize_repair_state(state: RepairState) -> None:
    """统一收尾：终态 status + failure_tags。"""
    from src.repair.failure_tags import apply_failure_tags

    apply_terminal_status(state)
    apply_failure_tags(state)
