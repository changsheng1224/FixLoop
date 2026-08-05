"""自适应预算：按锚点强度与信息增益分配 localize/patch 算力。

相对纯墙钟阶段预算：
- 规则定位已接地 → LLM localize 变为限时 enrich，超时丢弃
- 零增益 / 已否定假设 → 下调 patcher 工具步数，少烧无效轮
- 短修快路径仍可抬高步数，但受增益惩罚约束
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.state import RepairState, SuspectLocation

__all__ = [
    "AdaptiveBudgetAdvice",
    "advise_budget",
    "localize_enrich_timeout_s",
    "recommend_patcher_steps",
    "reserve_patch_budget_s",
    "should_skip_llm_localize",
]

_DEFAULT_ENRICH_S = 25.0
_DEFAULT_ENRICH_WEAK_S = 45.0
_DEFAULT_PATCH_STEPS = 12
_SHORT_PATCH_STEPS = 16


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class AdaptiveBudgetAdvice:
    skip_llm_localize: bool
    localize_enrich_s: float
    patcher_max_steps: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "skip_llm_localize": self.skip_llm_localize,
            "localize_enrich_s": self.localize_enrich_s,
            "patcher_max_steps": self.patcher_max_steps,
            "reason": self.reason,
        }


def should_skip_llm_localize(
    rule_suspects: list["SuspectLocation"] | None,
    *,
    grounded: bool,
    related_tests: list[str] | None = None,
) -> bool:
    """规则已强锚定时，跳过 LLM localize（仍可跑检索）。"""
    force = (os.environ.get("FIXLOOP_SKIP_LLM_LOCALIZE") or "").strip().lower()
    if force in ("1", "true", "yes", "on"):
        return True
    if force in ("0", "false", "no", "off"):
        return False
    n = len(rule_suspects or [])
    tests = [t for t in (related_tests or []) if t]
    # 已有 grounded 实现 + 足够规则嫌疑（或已有测试锚）→ 不必再烧 localize LLM
    if grounded and n >= 1 and (n >= 2 or tests):
        return True
    return False


def localize_enrich_timeout_s(*, grounded: bool, rule_count: int = 0) -> float:
    """LLM localize 作为 enrich 的硬超时（秒）。"""
    weak = _env_float("FIXLOOP_LOCALIZE_ENRICH_WEAK_S", _DEFAULT_ENRICH_WEAK_S)
    strong = _env_float("FIXLOOP_LOCALIZE_ENRICH_S", _DEFAULT_ENRICH_S)
    if grounded and rule_count > 0:
        return max(5.0, strong)
    return max(8.0, weak)


def recommend_patcher_steps(
    state: "RepairState | None" = None,
    *,
    base_steps: int | None = None,
    short_repair: bool = False,
) -> int:
    """按信息增益 / 失败账本下调或维持 patcher 工具步。"""
    base = int(base_steps or _env_int("FIXLOOP_PATCHER_MAX_STEPS", _DEFAULT_PATCH_STEPS))
    if short_repair:
        base = max(base, _env_int("FIXLOOP_SHORT_REPAIR_STEPS", _SHORT_PATCH_STEPS))

    zero_gain = 0
    negated = 0
    if state is not None:
        ig = state.node_timings.get("info_gain") or {}
        if isinstance(ig, dict):
            zero_gain = int(ig.get("zero_gain_streak") or 0)
        ledger = state.node_timings.get("failure_ledger") or {}
        if isinstance(ledger, dict):
            negated = len(ledger.get("negated_files") or [])

    steps = base
    if zero_gain >= 2:
        steps = max(6, steps - 4)
    elif zero_gain >= 1:
        steps = max(8, steps - 2)
    if negated >= 2:
        steps = max(6, steps - 2)
    return int(steps)


def advise_budget(
    state: "RepairState | None",
    *,
    rule_suspects: list["SuspectLocation"] | None = None,
    grounded: bool = False,
    related_tests: list[str] | None = None,
    short_repair: bool = False,
    base_patch_steps: int | None = None,
) -> AdaptiveBudgetAdvice:
    skip = should_skip_llm_localize(
        rule_suspects, grounded=grounded, related_tests=related_tests
    )
    enrich_s = localize_enrich_timeout_s(
        grounded=grounded, rule_count=len(rule_suspects or [])
    )
    steps = recommend_patcher_steps(
        state, base_steps=base_patch_steps, short_repair=short_repair
    )
    if skip:
        reason = "rule_grounded_skip_llm_localize"
    elif grounded:
        reason = "rule_grounded_enrich_budget"
    else:
        reason = "weak_anchor_full_enrich"
    return AdaptiveBudgetAdvice(
        skip_llm_localize=skip,
        localize_enrich_s=enrich_s,
        patcher_max_steps=steps,
        reason=reason,
    )


def reserve_patch_budget_s(repair_total_s: int) -> dict[str, int]:
    """与 PhaseTimeoutConfig.with_repair_total_cap 对齐的预算摘要（供测试/诊断）。"""
    from src.repair.phase_clock import PhaseTimeoutConfig

    cfg = PhaseTimeoutConfig.with_repair_total_cap(int(repair_total_s or 0))
    return cfg.budget_dict()
