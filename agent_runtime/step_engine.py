"""Shared ReAct step lifecycle adapter used by native and XML loops."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from agent_runtime.runtime_contracts import RuntimePhase


@dataclass(frozen=True)
class StepEvent:
    phase: str
    step: int
    path: str
    tool: str = ""


class StepEngine:
    """Project protocol-specific phases onto one Runtime lifecycle."""

    _PHASES = {
        "reasoning": RuntimePhase.REASONING,
        "acting": RuntimePhase.ACTING,
        "observation": RuntimePhase.OBSERVING,
    }

    def __init__(self, task_state, emit: Callable[[str, dict], None]):
        self.task_state = task_state
        self.emit = emit

    def enter(self, phase: str, *, step: int, path: str, tool: str = "") -> StepEvent:
        event = StepEvent(str(phase), int(step), str(path), str(tool or ""))
        self.task_state.phase = event.phase
        self.task_state.turn = event.step
        lifecycle_phase = self._PHASES.get(event.phase)
        if lifecycle_phase is not None:
            try:
                self.task_state.advance_runtime(lifecycle_phase)
            except ValueError as exc:
                self.emit(
                    "runtime_contract_violation",
                    {
                        "phase": event.phase,
                        "runtime_phase": lifecycle_phase.value,
                        "detail": str(exc),
                        "step": event.step,
                    },
                )
        return event


__all__ = ["StepEngine", "StepEvent"]
