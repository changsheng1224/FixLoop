"""Explicit Repair phase transitions and resumable safe points."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from src.state import RepairPhase

SAFE_POINTS = frozenset(
    {"intent", "context", "localization", "patch", "verify", "recovery", "done", "failed"}
)

_ALLOWED: dict[str, set[str]] = {
    "": {"intent", "seed", "localize", "context", "patch", "verify", "recovery"},
    "intent": {"context", "localize", "patch", "recovery"},
    "seed": {"localize", "context", "patch", "verify", "recovery"},
    "localize": {"seed", "context", "patch", "verify", "recovery", "failed"},
    "context": {"seed", "localize", "patch", "verify", "recovery", "failed"},
    # Legacy persisted states used ``retrieve`` for the context phase.
    "retrieve": {"seed", "localize", "patch", "verify", "recovery", "failed"},
    "patch": {"verify", "patch", "recovery", "done", "failed"},
    "verify": {"patch", "verify", "recovery", "done", "failed"},
    "recovery": {"seed", "patch", "verify", "context", "done", "failed"},
    "done": set(),
    "failed": set(),
}


@dataclass(frozen=True)
class PhaseTransition:
    from_phase: str
    to_phase: str
    revision: int
    reason: str = ""
    at: float = 0.0
    valid: bool = True
    actor: str = "runtime"
    correlation_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class RepairPhaseFSM:
    def __init__(self, phase: str = "", revision: int = 0, history=None):
        self.phase = str(phase or "")
        self.revision = int(revision or 0)
        self.history: list[dict[str, Any]] = list(history or [])

    def transition(
        self,
        to_phase: str,
        reason: str = "",
        *,
        allow_recovery: bool = False,
        actor: str = "runtime",
        correlation_id: str = "",
    ) -> PhaseTransition:
        target = str(to_phase or "")
        if target not in {phase.value for phase in RepairPhase}:
            transition = PhaseTransition(
                from_phase=self.phase,
                to_phase=target,
                revision=self.revision,
                reason="unknown_phase",
                at=time.time(),
                valid=False,
                actor=actor,
                correlation_id=correlation_id,
            )
            self.history.append(transition.to_dict())
            return transition
        allowed = target in _ALLOWED.get(self.phase, set())
        if allow_recovery and target == "recovery":
            allowed = True
        transition = PhaseTransition(
            from_phase=self.phase,
            to_phase=target,
            revision=self.revision + 1,
            reason=str(reason or ""),
            at=time.time(),
            valid=allowed,
            actor=actor,
            correlation_id=correlation_id,
        )
        if allowed:
            self.phase = target
            self.revision += 1
        self.history.append(transition.to_dict())
        self.history = self.history[-100:]
        return transition

    def snapshot(self) -> dict[str, Any]:
        return {"phase": self.phase, "revision": self.revision, "history": list(self.history)}

    @classmethod
    def from_state(cls, state) -> RepairPhaseFSM:
        snapshot = (getattr(state, "node_timings", {}) or {}).get("phase_fsm") or {}
        return cls(
            phase=str(getattr(state, "phase", "") or snapshot.get("phase", "")),
            revision=int(snapshot.get("revision", 0) or 0),
            history=snapshot.get("history", []),
        )

    def apply(
        self,
        state,
        to_phase: str,
        reason: str = "",
        *,
        allow_recovery: bool = False,
        actor: str = "runtime",
        correlation_id: str = "",
    ) -> PhaseTransition:
        transition = self.transition(
            to_phase,
            reason,
            allow_recovery=allow_recovery,
            actor=actor,
            correlation_id=correlation_id,
        )
        if transition.valid:
            state.phase = RepairPhase(transition.to_phase)
            state.state_revision = max(
                int(getattr(state, "state_revision", 0)), transition.revision
            )
        state.phase_history = list(self.history)
        state.node_timings["phase_fsm"] = self.snapshot()
        return transition


def resume_phase_for_invalidated_workspace(state) -> str:
    """Choose the earliest safe phase after workspace evidence invalidation."""
    if getattr(state, "repair_plan", None) is None:
        return "intent"
    if getattr(state, "suspect_locations", None):
        return "context"
    return "localize"
