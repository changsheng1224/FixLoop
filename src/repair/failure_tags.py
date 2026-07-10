"""Repair 失败根因分类 tag（badcase metadata 用）。"""

from __future__ import annotations

from enum import StrEnum

from src.repair.termination import RepairTerminalStatus, introduced_regression, is_repair_success
from src.state import RepairState

__all__ = [
    "FailureTag",
    "allowed_patch_files",
    "apply_failure_tags",
    "classify_failure_tags",
]


class FailureTag(StrEnum):
    PARSE_FAIL = "parse_fail"
    WRONG_FILE = "wrong_file"
    REGRESSION = "regression"
    TIMEOUT = "timeout"


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def allowed_patch_files(state: RepairState) -> set[str]:
    """补丁允许落盘的文件集合（suspect + plan + related_tests）。"""
    allowed: set[str] = set()
    for suspect in state.suspect_locations:
        if suspect.file_path:
            allowed.add(_normalize_path(suspect.file_path))
    if state.repair_plan:
        for file_path in state.repair_plan.suspect_files:
            if file_path:
                allowed.add(_normalize_path(file_path))
    if state.retrieved_context:
        for test_ref in state.retrieved_context.related_tests:
            file_part = test_ref.split("::", 1)[0].strip()
            if file_part.endswith(".py"):
                allowed.add(_normalize_path(file_part))
    return allowed


def _is_timeout(state: RepairState) -> bool:
    if state.status == RepairTerminalStatus.TIMEOUT:
        return True
    if state.node_timings.get("repair_timeout"):
        return True
    orch_err = state.agent_errors.get("orchestrator", "")
    return "repair timeout" in orch_err


def _is_parse_fail(state: RepairState) -> bool:
    if state.node_timings.get("patcher_parse_failed"):
        return True
    if state.candidate_patches:
        return False
    if state.agent_errors.get("patcher_apply"):
        return False
    return state.retry_count > 0 or state.status in (
        RepairTerminalStatus.EXHAUSTED,
        RepairTerminalStatus.FAILED,
    )


def _is_wrong_file(state: RepairState) -> bool:
    if not state.candidate_patches:
        return False
    allowed = allowed_patch_files(state)
    if not allowed:
        return False
    patch_paths = {_normalize_path(p.file_path) for p in state.candidate_patches if p.file_path}
    if not patch_paths:
        return False
    return patch_paths.isdisjoint(allowed)


def classify_failure_tags(state: RepairState) -> list[str]:
    """按优先级推断主失败 tag；成功修复返回空列表。"""
    if is_repair_success(state):
        return []
    if _is_timeout(state):
        return [FailureTag.TIMEOUT]
    if state.status == RepairTerminalStatus.REGRESSION or introduced_regression(state):
        return [FailureTag.REGRESSION]
    if _is_parse_fail(state):
        return [FailureTag.PARSE_FAIL]
    if _is_wrong_file(state):
        return [FailureTag.WRONG_FILE]
    return []


def apply_failure_tags(state: RepairState) -> None:
    """将 classify 结果写入 RepairState.failure_tags。"""
    state.failure_tags = classify_failure_tags(state)
