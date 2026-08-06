"""Repair 失败根因分类 tag（badcase metadata 用）。"""

from __future__ import annotations

from enum import StrEnum

from src.repair.verification.termination import (
    RepairTerminalStatus,
    has_repair_timeout,
    introduced_regression,
    is_repair_success,
)
from src.state import RepairState

__all__ = [
    "FailureTag",
    "allowed_patch_files",
    "apply_failure_tags",
    "check_patch_faithfulness",
    "classify_failure_tags",
    "promote_paths_to_suspects",
]


class FailureTag(StrEnum):
    PARSE_FAIL = "parse_fail"
    APPLY_FAILED = "apply_failed"
    WRONG_FILE = "wrong_file"
    REGRESSION = "regression"
    TIMEOUT = "timeout"
    VERIFY_CONFIG = "verify_config"
    NO_PROGRESS = "no_progress"


DEGRADED_BASELINE_TAG = "degraded_baseline"

# 非失败根因、仅描述修复路径的 metadata tag（成功时仍保留）
REPAIR_METADATA_TAGS = frozenset({DEGRADED_BASELINE_TAG})


def _is_verify_config_failure(state: RepairState) -> bool:
    """空收集 / pip / 依赖 / Django settings 等：验证环境问题，不是补丁语义失败。"""
    from src.repair.verification.verify_diagnose import VerifyBucket, diagnose_verification

    vr = state.verification_result
    if vr is None:
        return False
    diag = diagnose_verification(vr)
    if diag.bucket == VerifyBucket.ENV:
        return True
    if vr.total_tests == 0 and not vr.all_passed:
        logs = "\n".join(str(x) for x in (vr.failure_logs or []))
        markers = (
            "verify_config:",
            "未收集到任何测试",
            "sandbox pip install failed",
            "mkdir: unrecognized option",
        )
        return any(m in logs for m in markers)
    return False


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _repo_root_hint(state: RepairState) -> str:
    return str(state.node_timings.get("_repo_root_hint") or "").strip()


def allowed_patch_files(state: RepairState) -> set[str]:
    """补丁允许落盘的文件集合（suspect + plan + related_tests + F2P/test_patch）。

    与 bonus §24 faithfulness（patch ⊆ allowed）共用；勿重复实现第二套集合逻辑。
    """
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
    for extra in state.node_timings.get("allowed_patch_extra") or []:
        if extra:
            allowed.add(_normalize_path(str(extra)))

    root = _repo_root_hint(state)
    test_patch = str(state.node_timings.get("verify_test_patch") or "")
    if test_patch and root:
        try:
            from src.repair.localization.localize_test_patch import suspects_from_test_patch

            for s in suspects_from_test_patch(test_patch, root, max_keep=16):
                if s.file_path:
                    allowed.add(_normalize_path(s.file_path))
        except Exception:
            pass
    if state.issue_input and root:
        try:
            from src.repair.localization.localize_fastpath import suspects_from_fail_to_pass

            for s in suspects_from_fail_to_pass(
                state.issue_input, root, max_keep=8
            ):
                if s.file_path:
                    allowed.add(_normalize_path(s.file_path))
        except Exception:
            pass
    return allowed


def promote_paths_to_suspects(
    state: RepairState,
    paths: list[str],
    *,
    repo_root: str = "",
    reason: str = "faithfulness_promoted",
) -> list[str]:
    """把闸口拒绝但磁盘存在的实现文件晋升为 suspect，并写入 allowed_extra。"""
    from src.repair.path_resolve import is_impl_py_path, resolve_repo_relpath
    from src.state import SuspectLocation

    root = repo_root or _repo_root_hint(state)
    if not root:
        return []
    existing = {
        _normalize_path(s.file_path)
        for s in (state.suspect_locations or [])
        if s.file_path
    }
    extras = list(state.node_timings.get("allowed_patch_extra") or [])
    promoted: list[str] = []
    for raw in paths:
        rel = resolve_repo_relpath(root, raw)
        if not rel or not is_impl_py_path(rel):
            continue
        if rel in existing:
            if rel not in extras:
                extras.append(rel)
            continue
        state.suspect_locations = list(state.suspect_locations or []) + [
            SuspectLocation(
                file_path=rel,
                start_line=1,
                end_line=1,
                reason=reason,
                confidence=0.72,
            )
        ]
        existing.add(rel)
        if rel not in extras:
            extras.append(rel)
        promoted.append(rel)
    if extras:
        state.node_timings["allowed_patch_extra"] = extras
    if promoted:
        state.node_timings["faithfulness_promoted"] = promoted
    return promoted


def _is_apply_fail(state: RepairState) -> bool:
    if state.agent_errors.get("patcher_apply"):
        return True
    return bool(state.node_timings.get("patcher_apply_failed"))


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
    """任一 patch 文件不在 allowed 集合中 → 幻觉/改错文件。"""
    if not state.candidate_patches:
        return False
    allowed = allowed_patch_files(state)
    if not allowed:
        return False
    patch_paths = {_normalize_path(p.file_path) for p in state.candidate_patches if p.file_path}
    if not patch_paths:
        return False
    # patch 文件必须 ⊆ allowed（任一不在即为 wrong_file）
    return not patch_paths.issubset(allowed)


def classify_failure_tags(state: RepairState) -> list[FailureTag]:
    """按优先级推断主失败 tag；成功修复返回空列表。"""
    if is_repair_success(state):
        return []
    if has_repair_timeout(state):
        return [FailureTag.TIMEOUT]
    if state.status == RepairTerminalStatus.REGRESSION or introduced_regression(state):
        return [FailureTag.REGRESSION]
    if _is_apply_fail(state):
        return [FailureTag.APPLY_FAILED]
    # E17: 空收集/环境失败优先于笼统 parse_fail（有候选但 verify 配置坏时）
    if _is_verify_config_failure(state) and state.candidate_patches:
        return [FailureTag.VERIFY_CONFIG]
    stop_reason = str(state.node_timings.get("stop_loss") or "")
    if stop_reason == "env" or (
        state.node_timings.get("verify_env_early_stop") and _is_verify_config_failure(state)
    ):
        return [FailureTag.VERIFY_CONFIG]
    if stop_reason or state.node_timings.get("stop_loss_early"):
        if stop_reason in ("apply_thrash",) or _is_apply_fail(state):
            return [FailureTag.APPLY_FAILED]
        if stop_reason in ("parse_thrash",) or _is_parse_fail(state):
            return [FailureTag.PARSE_FAIL]
        return [FailureTag.NO_PROGRESS]
    if _is_parse_fail(state):
        return [FailureTag.PARSE_FAIL]
    if _is_verify_config_failure(state):
        return [FailureTag.VERIFY_CONFIG]
    if _is_wrong_file(state):
        return [FailureTag.WRONG_FILE]
    return []


def check_patch_faithfulness(
    patches: list,
    state,
    *,
    soft_keep: bool = True,
    repo_root: str = "",
) -> tuple[list, list[str]]:
    """闸口：过滤掉操作无关文件的幻觉 patch。

    soft_keep=True（默认）：
    - 拒绝前若路径在 repo 内真实存在且为实现 .py → 晋升并放行
    - 过滤后若 kept 为空但原 patch 非空 → 保留磁盘上存在的实现文件 patch，打标 faithfulness_soft
    """
    from src.repair.path_resolve import is_impl_py_path, resolve_repo_relpath

    allowed = allowed_patch_files(state)
    if not allowed:
        return list(patches), []

    root = repo_root or _repo_root_hint(state)

    def _canonical(path: str) -> str:
        if root:
            resolved = resolve_repo_relpath(root, path)
            if resolved:
                return resolved
        return _normalize_path(path)

    kept: list = []
    rejected: list[str] = []
    for p in patches:
        path = _canonical(p.file_path) if p.file_path else ""
        if path and path not in allowed:
            rejected.append(path)
        else:
            if path and getattr(p, "file_path", None) and path != _normalize_path(p.file_path):
                p.file_path = path
            kept.append(p)

    if rejected and soft_keep and root:
        promoted = promote_paths_to_suspects(state, rejected, repo_root=root)
        if promoted:
            allowed = allowed_patch_files(state)
            kept2: list = []
            rejected2: list[str] = []
            for p in patches:
                path = _canonical(p.file_path) if p.file_path else ""
                if path and path not in allowed:
                    rejected2.append(path)
                else:
                    if path:
                        p.file_path = path
                    kept2.append(p)
            kept, rejected = kept2, rejected2

    if soft_keep and patches and not kept and root:
        rescued: list = []
        still: list[str] = []
        for p in patches:
            path = _canonical(p.file_path) if p.file_path else ""
            if path and is_impl_py_path(path) and resolve_repo_relpath(root, path):
                p.file_path = path
                rescued.append(p)
            elif path:
                still.append(path)
        if rescued:
            state.node_timings["faithfulness_soft"] = True
            promote_paths_to_suspects(
                state,
                [p.file_path for p in rescued if p.file_path],
                repo_root=root,
                reason="faithfulness_soft",
            )
            return rescued, still
    return kept, rejected


def apply_failure_tags(state: RepairState) -> None:
    """将 classify 结果写入 RepairState.failure_tags（字符串值）。"""
    preserved = [tag for tag in state.failure_tags if tag in REPAIR_METADATA_TAGS]
    state.failure_tags = [tag.value for tag in classify_failure_tags(state)] + preserved
