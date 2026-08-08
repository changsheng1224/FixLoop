"""Latency SLO observation and adaptive degradation decisions."""

from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Any


class LatencySLOController:
    def __init__(self, config=None, degradation=None):
        self.slo = config or {}
        self.degradation = degradation or {}
        self.samples: dict[str, list[int]] = defaultdict(list)
        self.started_at = time.monotonic()
        self.degraded = False
        self.last_decision: dict[str, Any] = {}

    def record(self, kind: str, duration_ms: int) -> None:
        values = self.samples[str(kind)]
        values.append(max(0, int(duration_ms)))
        del values[:-100]

    @staticmethod
    def _p95(values: list[int]) -> int:
        if not values:
            return 0
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))]

    def summary(self) -> dict[str, Any]:
        return {
            "samples": {key: len(values) for key, values in self.samples.items()},
            "p95_ms": {key: self._p95(values) for key, values in self.samples.items()},
            "degraded": self.degraded,
            "last_decision": dict(self.last_decision),
        }

    def decide(self, *, remaining_s: float | None, max_output_tokens: int) -> dict[str, Any]:
        actions: list[str] = []
        reasons: list[str] = []
        ttft_target = int(getattr(self.slo, "ttft_p95_ms", 0) or 0)
        model_target = int(getattr(self.slo, "model_p95_ms", 0) or 0)
        if ttft_target and self._p95(self.samples.get("ttft", [])) > ttft_target:
            actions.append("reduce_output_tokens")
            reasons.append("ttft_p95_exceeded")
        if model_target and self._p95(self.samples.get("model", [])) > model_target:
            actions.extend(("reduce_output_tokens", "skip_optional_context"))
            reasons.append("model_p95_exceeded")
        if remaining_s is not None and remaining_s <= 0:
            actions.append("stop")
            reasons.append("deadline_exhausted")
        elif remaining_s is not None and remaining_s < 30:
            actions.extend(("reduce_output_tokens", "skip_optional_context"))
            reasons.append("deadline_tight")
        enabled = bool(getattr(self.degradation, "enabled", True))
        actions = list(dict.fromkeys(actions)) if enabled else []
        next_tokens = int(max_output_tokens)
        floor = int(getattr(self.degradation, "max_output_floor", 512) or 512)
        if "reduce_output_tokens" in actions:
            next_tokens = max(floor, min(next_tokens, max(floor, next_tokens // 2)))
        decision = {
            "degraded": bool(actions),
            "actions": actions,
            "reasons": reasons,
            "max_output_tokens": next_tokens,
            "remaining_s": remaining_s,
        }
        self.degraded = self.degraded or bool(actions)
        self.last_decision = decision
        return decision


__all__ = ["LatencySLOController"]
