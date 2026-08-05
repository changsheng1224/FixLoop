"""可恢复长程：策略阶段机（收敛 → 扩搜/换假设 → 再收敛）。

与止损配合：
- env 不可策略恢复，直接止损
- identical_* / no_progress / thrash 可消耗有限次 strategy shift
- shift 后重置软止损计数，注入新反馈，允许继续烧剩余 retry
- 状态写入 node_timings，供 checkpoint/resume 恢复
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.repair.stop_loss import StopLossDecision, StopLossTracker
    from src.state import RepairState

__all__ = [
    "LongHorizonController",
    "StrategyDecision",
    "StrategyPhase",
    "apply_horizon_to_state",
    "load_horizon_from_state",
    "strategy_feedback",
]


class StrategyPhase(StrEnum):
    CONVERGE = "converge"
    EXPAND_SEARCH = "expand_search"
    SWITCH_HYPOTHESIS = "switch_hypothesis"
    RECONVERGE = "reconverge"
    EXHAUSTED = "exhausted"


# 可通过策略切换尝试恢复的止损原因
_RECOVERABLE_REASONS = frozenset(
    {
        "identical_patch",
        "identical_verify",
        "no_progress",
        "apply_thrash",
        "parse_thrash",
    }
)


@dataclass(frozen=True)
class StrategyDecision:
    action: str  # "continue" | "shift" | "stop"
    phase: StrategyPhase = StrategyPhase.CONVERGE
    reason: str = ""
    hint: str = ""


@dataclass
class LongHorizonController:
    max_shifts: int = 2
    shifts_used: int = 0
    phase: StrategyPhase = StrategyPhase.CONVERGE
    history: list[str] = field(default_factory=list)

    def on_stop_signal(self, reason: str) -> StrategyDecision:
        """止损信号到来时：尝试策略切换，否则停机。"""
        reason = str(reason or "")
        if reason == "env" or reason not in _RECOVERABLE_REASONS:
            self.phase = StrategyPhase.EXHAUSTED
            return StrategyDecision(
                action="stop",
                phase=self.phase,
                reason=reason or "unrecoverable",
                hint="不可恢复的验证/环境失败，结束长程。",
            )
        if self.shifts_used >= self.max_shifts:
            self.phase = StrategyPhase.EXHAUSTED
            return StrategyDecision(
                action="stop",
                phase=self.phase,
                reason=reason,
                hint=f"已用尽 {self.max_shifts} 次策略切换，止损。",
            )

        if reason in ("identical_verify", "no_progress"):
            next_phase = StrategyPhase.SWITCH_HYPOTHESIS
            hint = (
                "策略切换：换假设。丢弃重复失败面，重新锚定嫌疑文件/函数，"
                "并强制扩展检索后再收敛。"
            )
        else:
            next_phase = StrategyPhase.EXPAND_SEARCH
            hint = (
                "策略切换：扩搜。在现有嫌疑附近强制探索更多测试与调用方，"
                "避免重复同一补丁路径。"
            )

        self.shifts_used += 1
        self.phase = next_phase
        self.history.append(f"{reason}->{next_phase.value}")
        return StrategyDecision(
            action="shift",
            phase=next_phase,
            reason=reason,
            hint=hint,
        )

    def mark_reconverge(self) -> None:
        if self.phase != StrategyPhase.EXHAUSTED:
            self.phase = StrategyPhase.RECONVERGE

    def to_dict(self) -> dict:
        return {
            "max_shifts": self.max_shifts,
            "shifts_used": self.shifts_used,
            "phase": self.phase.value,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "LongHorizonController":
        data = data or {}
        phase_raw = str(data.get("phase") or StrategyPhase.CONVERGE.value)
        try:
            phase = StrategyPhase(phase_raw)
        except ValueError:
            phase = StrategyPhase.CONVERGE
        return cls(
            max_shifts=int(data.get("max_shifts", 2) or 2),
            shifts_used=int(data.get("shifts_used", 0) or 0),
            phase=phase,
            history=list(data.get("history") or []),
        )


def apply_horizon_to_state(state: "RepairState", ctrl: LongHorizonController) -> None:
    state.node_timings["long_horizon"] = ctrl.to_dict()


def load_horizon_from_state(state: "RepairState") -> LongHorizonController:
    raw = state.node_timings.get("long_horizon")
    if isinstance(raw, dict):
        return LongHorizonController.from_dict(raw)
    return LongHorizonController()


def strategy_feedback(decision: StrategyDecision) -> str:
    extra = ""
    if decision.phase == StrategyPhase.EXPAND_SEARCH:
        extra = (
            "\n动作: 扩大搜索——grep/read 相邻模块与调用方；"
            "不要在同一文件重复相同 hunk。"
        )
    elif decision.phase == StrategyPhase.SWITCH_HYPOTHESIS:
        extra = (
            "\n动作: 换假设——放弃当前文件/符号；"
            "先 read_file 打开失败测试，再定位另一实现点；禁止再提交相同 fingerprint。"
        )
    elif decision.phase == StrategyPhase.RECONVERGE:
        extra = "\n动作: 收敛——对当前嫌疑做最小 diff，对照失败断言。"
    return (
        f"[长程策略] phase={decision.phase.value}; trigger={decision.reason}\n"
        f"{decision.hint}{extra}"
    )


def reset_stop_loss_tracker(tracker: "StopLossTracker") -> "StopLossTracker":
    """策略切换后重置软计数，保留阈值配置。"""
    from src.repair.stop_loss import StopLossTracker

    return StopLossTracker(
        identical_patch_threshold=tracker.identical_patch_threshold,
        identical_verify_threshold=tracker.identical_verify_threshold,
        apply_thrash_threshold=tracker.apply_thrash_threshold,
        parse_thrash_threshold=tracker.parse_thrash_threshold,
        no_progress_threshold=tracker.no_progress_threshold,
        env_threshold=tracker.env_threshold,
    )


def clear_soft_stop_flags(state: "RepairState") -> None:
    """策略切换后清掉早停标记，避免误判 exhausted。"""
    state.node_timings.pop("stop_loss", None)
    state.node_timings.pop("stop_loss_early", None)
    state.agent_errors.pop("stop_loss", None)
    # 允许新一轮补丁指纹
    state.node_timings.pop("patch_retry_fingerprints", None)
