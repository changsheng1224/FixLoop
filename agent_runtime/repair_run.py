"""Shared repair-run contracts for streaming, cancellation and attribution."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RepairPhase(StrEnum):
    INTENT = "intent"
    CONTEXT = "context"
    LOCALIZATION = "localization"
    TOOL = "tool"
    PATCH = "patch"
    VERIFICATION = "verification"
    RECOVERY = "recovery"
    CHECKPOINT = "checkpoint"
    FINALIZATION = "finalization"


class CancelKind(StrEnum):
    USER = "user"
    DEADLINE = "deadline"
    PROVIDER = "provider"
    TOOL = "tool"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class CancellationCause:
    kind: str
    source: str = "runtime"
    requested_at: float = field(default_factory=time.time)
    reason: str = ""
    graceful: bool = True

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class StreamEvent:
    run_id: str
    kind: str
    phase: str = ""
    turn: int = 0
    payload: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    terminal: bool = False
    replayable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "seq": self.seq,
            "kind": self.kind,
            "phase": self.phase,
            "turn": self.turn,
            "payload": dict(self.payload),
            "terminal": self.terminal,
            "replayable": self.replayable,
        }


class RunTerminalGuard:
    """First terminal decision wins; later timeout/cancel races are recorded only."""

    def __init__(self):
        self.status = ""
        self.event: StreamEvent | None = None

    def try_finish(self, run_id: str, status: str, payload: dict[str, Any] | None = None) -> bool:
        if self.status:
            return False
        self.status = status
        self.event = StreamEvent(
            run_id=run_id,
            kind="run_terminal",
            phase=RepairPhase.FINALIZATION.value,
            payload=payload or {},
            terminal=True,
        )
        return True


_ATTRIBUTION_RULES = (
    (
        "tool",
        {"invalid_args", "path_escape", "permission_denied", "quota_exceeded", "tool_timeout"},
    ),
    ("patch", {"patch_parse_error", "patch_apply_error", "invalid_patch"}),
    (
        "verification",
        {"verification_failed", "verification_timeout", "verification_environment_failure"},
    ),
    ("context", {"context_overflow", "context_integrity_failure"}),
    ("localization", {"no_hypothesis_convergence", "no_relevant_evidence"}),
)


def attribute_failure(
    *,
    stop_reason: str = "",
    observations: list[dict[str, Any]] | None = None,
    changed_files: list[str] | None = None,
    evidence_count: int = 0,
) -> dict[str, Any]:
    """Create evidence-backed attribution; never decides a code patch."""
    observations = observations or []
    for item in reversed(observations):
        failure = str(item.get("failure_class", "") or item.get("failure_type", ""))
        for phase, values in _ATTRIBUTION_RULES:
            if failure in values:
                return {
                    "primary": f"{phase}_{failure}",
                    "secondary": [],
                    "confidence": 0.85,
                    "evidence": [
                        {
                            "kind": "tool_observation",
                            "observation_id": item.get("call_id", ""),
                            "signal": failure,
                        }
                    ],
                }
    if stop_reason in {"step_limit", "budget_exhausted"} and not changed_files:
        return {
            "primary": "localization_weak",
            "secondary": ["model_no_progress"],
            "confidence": 0.55,
            "evidence": [
                {
                    "kind": "runtime",
                    "signal": stop_reason,
                    "evidence_count": evidence_count,
                }
            ],
        }
    if stop_reason in {"user_cancel", "deadline_exceeded"}:
        return {
            "primary": f"runtime_{stop_reason}",
            "secondary": [],
            "confidence": 1.0,
            "evidence": [{"kind": "runtime", "signal": stop_reason}],
        }
    return {"primary": "unknown", "secondary": [], "confidence": 0.2, "evidence": []}
