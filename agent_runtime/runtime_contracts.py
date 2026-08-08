"""Explicit contracts for Agent Runtime lifecycle and terminal states.

The model remains responsible for repair decisions.  These types only make
runtime transitions, terminal evidence, and failure attribution explicit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RuntimePhase(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    REASONING = "reasoning"
    ACTING = "acting"
    OBSERVING = "observing"
    VERIFYING = "verifying"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RuntimeStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STOPPED = "stopped"


_ALLOWED_PHASES: dict[RuntimePhase, frozenset[RuntimePhase]] = {
    RuntimePhase.CREATED: frozenset(
        {RuntimePhase.PLANNING, RuntimePhase.REASONING, RuntimePhase.FINALIZING}
    ),
    RuntimePhase.PLANNING: frozenset(
        {RuntimePhase.REASONING, RuntimePhase.ACTING, RuntimePhase.FINALIZING}
    ),
    RuntimePhase.REASONING: frozenset({RuntimePhase.ACTING, RuntimePhase.FINALIZING}),
    RuntimePhase.ACTING: frozenset({RuntimePhase.OBSERVING, RuntimePhase.FINALIZING}),
    RuntimePhase.OBSERVING: frozenset(
        {RuntimePhase.REASONING, RuntimePhase.VERIFYING, RuntimePhase.FINALIZING}
    ),
    RuntimePhase.VERIFYING: frozenset({RuntimePhase.FINALIZING, RuntimePhase.REASONING}),
    RuntimePhase.FINALIZING: frozenset(
        {RuntimePhase.COMPLETED, RuntimePhase.FAILED, RuntimePhase.CANCELLED}
    ),
    RuntimePhase.COMPLETED: frozenset(),
    RuntimePhase.FAILED: frozenset(),
    RuntimePhase.CANCELLED: frozenset(),
}

_TERMINAL_PHASES = frozenset(
    {RuntimePhase.COMPLETED, RuntimePhase.FAILED, RuntimePhase.CANCELLED}
)


@dataclass
class RuntimeStateMachine:
    """Validate phase transitions and guarantee one terminal runtime state."""

    phase: RuntimePhase = RuntimePhase.CREATED
    status: RuntimeStatus = RuntimeStatus.RUNNING
    revision: int = 0
    stop_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.phase in _TERMINAL_PHASES

    def transition(
        self,
        phase: RuntimePhase | str,
        *,
        status: RuntimeStatus | str | None = None,
        stop_reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        target = RuntimePhase(str(phase))
        if self.terminal:
            if target == self.phase:
                return
            raise ValueError(f"terminal runtime cannot transition: {self.phase} -> {target}")
        if target != self.phase and target not in _ALLOWED_PHASES[self.phase]:
            raise ValueError(f"invalid runtime phase transition: {self.phase} -> {target}")
        if target in _TERMINAL_PHASES and not stop_reason:
            raise ValueError("terminal runtime transition requires stop_reason")
        self.phase = target
        if status is not None:
            self.status = RuntimeStatus(str(status))
        elif target == RuntimePhase.COMPLETED:
            self.status = RuntimeStatus.COMPLETED
        elif target == RuntimePhase.FAILED:
            self.status = RuntimeStatus.FAILED
        elif target == RuntimePhase.CANCELLED:
            self.status = RuntimeStatus.CANCELLED
        self.stop_reason = str(stop_reason or self.stop_reason)
        if metadata:
            self.metadata.update(metadata)
        self.revision += 1

    def terminal_contract(self) -> dict[str, Any]:
        """Return evidence required by report/checkpoint finalizers."""
        return {
            "phase": self.phase.value,
            "status": self.status.value,
            "terminal": self.terminal,
            "revision": self.revision,
            "stop_reason": self.stop_reason,
            "metadata": dict(self.metadata),
        }

    def snapshot(self) -> dict[str, Any]:
        return self.terminal_contract()

    @classmethod
    def from_snapshot(cls, data: dict[str, Any] | None) -> RuntimeStateMachine:
        raw = data or {}
        return cls(
            phase=RuntimePhase(str(raw.get("phase", RuntimePhase.CREATED.value))),
            status=RuntimeStatus(str(raw.get("status", RuntimeStatus.RUNNING.value))),
            revision=int(raw.get("revision", 0) or 0),
            stop_reason=str(raw.get("stop_reason", "") or ""),
            metadata=dict(raw.get("metadata") or {}),
        )


__all__ = ["RuntimePhase", "RuntimeStateMachine", "RuntimeStatus"]
