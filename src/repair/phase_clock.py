"""L2 repair 分阶段 wall-clock 预算（localize / patch / verify 累计）。"""

from __future__ import annotations

import time
from dataclasses import dataclass

__all__ = [
    "DEFAULT_LOCALIZE_TIMEOUT_S",
    "DEFAULT_PATCH_TIMEOUT_S",
    "DEFAULT_VERIFY_TIMEOUT_S",
    "PhaseTimeoutConfig",
    "PhaseTimeoutError",
    "RepairPhaseClock",
]

DEFAULT_LOCALIZE_TIMEOUT_S = 60
DEFAULT_PATCH_TIMEOUT_S = 90
DEFAULT_VERIFY_TIMEOUT_S = 120

_PHASES = ("localize", "patch", "verify")


class PhaseTimeoutError(Exception):
    """某 repair 阶段超出预算。"""

    def __init__(self, phase: str, budget_s: int, *, consumed_s: float = 0.0):
        self.phase = phase
        self.budget_s = int(budget_s)
        self.consumed_s = float(consumed_s)
        super().__init__(
            f"phase timeout ({phase}, {self.budget_s}s budget, consumed {self.consumed_s:.1f}s)"
        )


@dataclass(frozen=True)
class PhaseTimeoutConfig:
    """分阶段超时配置；各字段 ≤0 表示该维度禁用。"""

    localize_s: int = DEFAULT_LOCALIZE_TIMEOUT_S
    patch_s: int = DEFAULT_PATCH_TIMEOUT_S
    verify_s: int = DEFAULT_VERIFY_TIMEOUT_S
    repair_total_s: int = 0

    @classmethod
    def from_repair_timeout(cls, repair_timeout_s: int) -> PhaseTimeoutConfig:
        """由全局 ``repair_timeout_s`` 推导默认阶段预算。"""
        if repair_timeout_s <= 0:
            return cls(0, 0, 0, 0)
        return cls(repair_total_s=int(repair_timeout_s))

    def any_enabled(self) -> bool:
        return any(
            int(value) > 0
            for value in (
                self.localize_s,
                self.patch_s,
                self.verify_s,
                self.repair_total_s,
            )
        )

    def budget_dict(self) -> dict[str, int]:
        return {
            "localize_s": int(self.localize_s),
            "patch_s": int(self.patch_s),
            "verify_s": int(self.verify_s),
            "repair_total_s": int(self.repair_total_s),
        }


class RepairPhaseClock:
    """Orchestrator 阶段预算时钟；patch/verify 跨 retry 累计。"""

    def __init__(self, config: PhaseTimeoutConfig):
        self.config = config
        self._start = time.monotonic()
        self._consumed: dict[str, float] = {phase: 0.0 for phase in _PHASES}

    def ensure(self, phase: str) -> None:
        """进入阶段前检查剩余预算。"""
        self._check_repair_total()
        budget = self._budget(phase)
        if budget <= 0:
            return
        if self._consumed.get(phase, 0.0) >= budget:
            raise PhaseTimeoutError(phase, budget, consumed_s=self._consumed[phase])

    def consume(self, phase: str, elapsed_ms: int) -> None:
        """阶段完成后累计耗时并校验是否超预算。"""
        if phase not in self._consumed:
            return
        self._consumed[phase] += max(0, int(elapsed_ms)) / 1000.0
        budget = self._budget(phase)
        if budget > 0 and self._consumed[phase] > budget:
            raise PhaseTimeoutError(phase, budget, consumed_s=self._consumed[phase])
        self._check_repair_total()

    def consumed_snapshot(self) -> dict[str, float]:
        return dict(self._consumed)

    def _budget(self, phase: str) -> int:
        if phase == "localize":
            return int(self.config.localize_s)
        if phase == "patch":
            return int(self.config.patch_s)
        if phase == "verify":
            return int(self.config.verify_s)
        return 0

    def _check_repair_total(self) -> None:
        total_budget = int(self.config.repair_total_s)
        if total_budget <= 0:
            return
        elapsed = time.monotonic() - self._start
        if elapsed >= total_budget:
            raise PhaseTimeoutError("repair_total", total_budget, consumed_s=elapsed)
