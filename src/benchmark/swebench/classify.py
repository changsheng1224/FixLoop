"""失败归因：env / agent / eval。"""

from __future__ import annotations

from src.benchmark.swebench.types import FailureClass, InstanceResult


def classify_failure(
    *,
    env_error: str = "",
    agent_error: str = "",
    model_patch: str = "",
    resolved: bool | None = None,
    harness_error: str = "",
) -> tuple[FailureClass, str]:
    """按优先级归类。

    1. 环境（数据/镜像/checkout/harness 安装）
    2. Agent（无 patch / repair 失败）
    3. 评测（harness 判定未通过或 patch 不可用）
    """
    if env_error:
        return FailureClass.ENV, env_error
    if agent_error:
        return FailureClass.AGENT, agent_error
    if not (model_patch or "").strip():
        return FailureClass.AGENT, "empty_model_patch"
    if harness_error:
        return FailureClass.EVAL, harness_error
    if resolved is False:
        return FailureClass.EVAL, "not_resolved"
    if resolved is True:
        return FailureClass.NONE, ""
    return FailureClass.NONE, "pending_harness"


def classify_post_repair(
    *,
    model_patch: str,
    repair_status: str,
    verified: bool,
    skip_verify: bool = False,
) -> tuple[FailureClass, str]:
    """repair 结束后、harness 前的归因。

    - ``verified=True`` + fixed → ``pending_harness``（可进官方评测闸）
    - ``skip_verify`` + fixed + 合法 patch → ``pending_verify``（E15，不算 agent 失败）
    - timeout 且已有合法 unified patch → ``timeout_with_patch``（E14，可导出）
    - 无 patch / 非法导出 → agent
    """
    from src.benchmark.swebench.patch_export import looks_like_unified_diff

    patch = (model_patch or "").strip()
    status = (repair_status or "").strip().lower()
    if not patch:
        return FailureClass.AGENT, "empty_model_patch"
    if not looks_like_unified_diff(patch):
        return FailureClass.AGENT, "invalid_patch_format"
    if verified and status == "fixed":
        return FailureClass.NONE, "pending_harness"
    if status == "fixed" and (skip_verify or not verified):
        # E15: 主动跳过 verify 时记 pending，不进笼统 agent
        return FailureClass.NONE, "pending_verify"
    if status == "timeout":
        # E14: 有可导出 unified patch 的超时
        return FailureClass.NONE, "timeout_with_patch"
    if status != "fixed":
        return FailureClass.AGENT, "patch_without_fixed_status"
    return FailureClass.NONE, "pending_verify"


def apply_classification(result: InstanceResult) -> InstanceResult:
    fc, detail = classify_failure(
        env_error=result.error if result.failure_class == FailureClass.ENV else "",
        agent_error=(
            result.error
            if result.failure_class == FailureClass.AGENT
            else ("" if result.model_patch.strip() else result.error)
        ),
        model_patch=result.model_patch,
        resolved=result.resolved,
        harness_error=result.harness_log if result.failure_class == FailureClass.EVAL else "",
    )
    # 若调用方已写好 failure_class 且有 detail，保留；否则重算
    if result.failure_class != FailureClass.NONE and result.failure_detail:
        return result
    result.failure_class = fc
    result.failure_detail = detail or result.failure_detail
    return result


def summarize_failures(results: list[InstanceResult]) -> dict[str, int]:
    counts = {c.value: 0 for c in FailureClass}
    for r in results:
        counts[str(r.failure_class)] = counts.get(str(r.failure_class), 0) + 1
    return counts
