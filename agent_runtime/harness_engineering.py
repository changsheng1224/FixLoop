"""Provider-neutral Harness Engineering control plane.

This module owns run governance, not repair decisions.  It provides one
contract for lifecycle, events, budgets, human controls, attribution,
reproducible manifests and the Trace-to-Bad-Case feedback loop.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"


class HarnessStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    DEGRADED = "degraded"


class HarnessFailureCode(StrEnum):
    UNKNOWN = "unknown"
    INVALID_CONTRACT = "invalid_contract"
    TIMEOUT = "harness_timeout"
    CANCELLED = "harness_cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TOOL_PERMISSION = "tool_permission_denied"
    TOOL_INVALID_ARGUMENTS = "tool_invalid_arguments"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_EXECUTION = "tool_execution_failed"
    PATCH_CONFLICT = "patch_conflict"
    PATCH_INVALID = "patch_invalid"
    VERIFICATION_FAILED = "verification_failed"
    VERIFICATION_ENVIRONMENT = "verification_environment_failed"
    NO_PROGRESS = "no_progress"
    REPORT_INVALID = "report_invalid"
    RETRY_EXHAUSTED = "retry_exhausted"


class HumanControlMode(StrEnum):
    AUTO = "auto"
    READ_ONLY = "read_only"
    APPROVAL_REQUIRED = "approval_required"


class EvaluationLevel(StrEnum):
    CONTRACT = "contract"
    REGRESSION = "regression"
    BENCHMARK = "benchmark"
    OFFICIAL = "official"


@dataclass(frozen=True)
class ExecutionContract:
    """Versioned input boundary shared by every Harness participant."""

    run_id: str
    task_id: str = ""
    attempt: int = 0
    state_revision: int = 0
    phase: str = ""
    deadline_at: float = 0.0
    budget: dict[str, float] = field(default_factory=dict)
    input_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    manifest_fingerprint: str = ""

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.run_id:
            errors.append("run_id_required")
        if self.attempt < 0 or self.state_revision < 0:
            errors.append("revision_values_must_be_non_negative")
        if self.deadline_at and self.deadline_at <= time.time():
            errors.append("deadline_expired")
        for key, value in self.budget.items():
            if float(value) < 0:
                errors.append(f"budget_negative:{key}")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "state_revision": self.state_revision,
            "phase": self.phase,
            "deadline_at": self.deadline_at,
            "budget": dict(self.budget),
            "input_refs": list(self.input_refs),
            "evidence_refs": list(self.evidence_refs),
            "manifest_fingerprint": self.manifest_fingerprint,
        }


@dataclass(frozen=True)
class HarnessEvent:
    run_id: str
    event_type: str
    sequence: int
    status: str = ""
    phase: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    created_at: float = field(default_factory=time.time)
    replayable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "event_type": self.event_type,
            "sequence": self.sequence,
            "status": self.status,
            "phase": self.phase,
            "payload": dict(self.payload),
            "created_at": self.created_at,
            "replayable": self.replayable,
        }


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str
    reservation_id: str = ""
    remaining: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class FailureAttribution:
    primary_cause: str
    contributing_causes: tuple[str, ...] = ()
    confidence: float = 0.2
    evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_cause": self.primary_cause,
            "contributing_causes": list(self.contributing_causes),
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass
class HarnessMetrics:
    """Small cardinality-safe metrics accumulator for a single run."""

    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    event_count: int = 0
    tool_calls: int = 0
    retry_count: int = 0
    no_progress_count: int = 0
    token_count: int = 0
    cost_usd: float = 0.0
    phase_ms: dict[str, int] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)

    def observe(self, event_type: str, payload: dict[str, Any]) -> None:
        self.event_count += 1
        if event_type in {"tool_called", "tool_finished"}:
            self.tool_calls += 1 if event_type == "tool_called" else 0
        if event_type == "retry_started":
            self.retry_count += 1
        if event_type == "no_progress":
            self.no_progress_count += 1
        self.token_count = max(self.token_count, int(payload.get("total_tokens", 0) or 0))
        self.cost_usd = max(self.cost_usd, float(payload.get("cost_usd", 0.0) or 0.0))
        counter = payload.get("counter")
        if counter:
            key = str(counter)
            self.counters[key] = self.counters.get(key, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        finished = self.finished_at or time.time()
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": max(0, int((finished - self.started_at) * 1000)),
            "event_count": self.event_count,
            "tool_calls": self.tool_calls,
            "retry_count": self.retry_count,
            "no_progress_count": self.no_progress_count,
            "token_count": self.token_count,
            "cost_usd": self.cost_usd,
            "phase_ms": dict(self.phase_ms),
            "counters": dict(self.counters),
        }


_ALLOWED_TRANSITIONS = {
    HarnessStatus.CREATED: {HarnessStatus.RUNNING, HarnessStatus.CANCELLED},
    HarnessStatus.RUNNING: {
        HarnessStatus.PAUSED,
        HarnessStatus.CANCELLING,
        HarnessStatus.COMPLETED,
        HarnessStatus.FAILED,
        HarnessStatus.TIMED_OUT,
        HarnessStatus.DEGRADED,
    },
    HarnessStatus.PAUSED: {
        HarnessStatus.RUNNING,
        HarnessStatus.CANCELLING,
        HarnessStatus.CANCELLED,
    },
    HarnessStatus.CANCELLING: {HarnessStatus.CANCELLED, HarnessStatus.FAILED},
    HarnessStatus.DEGRADED: {
        HarnessStatus.RUNNING,
        HarnessStatus.COMPLETED,
        HarnessStatus.FAILED,
        HarnessStatus.CANCELLED,
    },
}
_TERMINAL = {
    HarnessStatus.COMPLETED,
    HarnessStatus.FAILED,
    HarnessStatus.CANCELLED,
    HarnessStatus.TIMED_OUT,
}


class HarnessControlPlane:
    """Thread-safe controller for one Agent Harness run."""

    def __init__(
        self,
        run_id: str,
        *,
        max_attempts: int = 1,
        budget_limits: dict[str, float] | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not run_id:
            raise ValueError("run_id is required")
        self.run_id = run_id
        self.max_attempts = max(1, int(max_attempts or 1))
        self.status = HarnessStatus.CREATED
        self.attempt = 0
        self.phase = ""
        self.stop_reason = ""
        self.failure: FailureAttribution | None = None
        self.control_mode = HumanControlMode.AUTO
        self._sequence = 0
        self._events: list[HarnessEvent] = []
        self._reservations: dict[str, dict[str, Any]] = {}
        self._limits = {str(k): max(0.0, float(v)) for k, v in (budget_limits or {}).items()}
        self._used = {key: 0.0 for key in self._limits}
        self._event_sink = event_sink
        self.metrics = HarnessMetrics()
        self._lock = threading.RLock()

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        status: str = "",
        replayable: bool = True,
    ) -> HarnessEvent:
        with self._lock:
            self._sequence += 1
            event = HarnessEvent(
                run_id=self.run_id,
                event_type=str(event_type),
                sequence=self._sequence,
                status=status or self.status.value,
                phase=self.phase,
                payload=dict(payload or {}),
                replayable=replayable,
            )
            self._events.append(event)
            self._events = self._events[-2000:]
            self.metrics.observe(event.event_type, event.payload)
            if self._event_sink is not None:
                self._event_sink(event.to_dict())
            return event

    def start(self, contract: ExecutionContract | None = None) -> bool:
        if contract is not None and contract.validate():
            self.finish(
                HarnessStatus.FAILED,
                reason=HarnessFailureCode.INVALID_CONTRACT.value,
            )
            return False
        with self._lock:
            if self.status == HarnessStatus.RUNNING:
                return True
            if self.status != HarnessStatus.CREATED:
                return False
            self.status = HarnessStatus.RUNNING
            self.attempt = max(1, self.attempt)
            self.emit("run_started", {"attempt": self.attempt})
            return True

    def transition(self, target: HarnessStatus | str, *, reason: str = "") -> bool:
        target = HarnessStatus(str(target))
        with self._lock:
            if target == self.status:
                return True
            if self.terminal:
                self.emit(
                    "run_terminal_late",
                    {"requested_status": target.value, "reason": reason},
                    status="late",
                    replayable=False,
                )
                return False
            if target not in _ALLOWED_TRANSITIONS.get(self.status, set()):
                return False
            previous = self.status
            self.status = target
            self.emit(
                "status_changed",
                {"from": previous.value, "to": target.value, "reason": reason},
            )
            return True

    def record_phase(self, phase: str, *, state_revision: int = 0, reason: str = "") -> bool:
        with self._lock:
            self.phase = str(phase)
            self.emit(
                "phase_changed",
                {"phase": self.phase, "state_revision": int(state_revision), "reason": reason},
            )
            return True

    def request_control(
        self,
        action: str,
        *,
        actor: str = "operator",
        reason: str = "",
        approval_token: str = "",
    ) -> bool:
        action = str(action).lower().strip()
        with self._lock:
            if action == "pause":
                accepted = self.transition(HarnessStatus.PAUSED, reason=reason or actor)
            elif action == "resume":
                accepted = self.transition(HarnessStatus.RUNNING, reason=reason or actor)
            elif action == "cancel":
                if self.terminal:
                    accepted = False
                else:
                    self.control_mode = HumanControlMode.READ_ONLY
                    accepted = self.transition(HarnessStatus.CANCELLING, reason=reason or actor)
            elif action == "readonly":
                self.control_mode = HumanControlMode.READ_ONLY
                accepted = True
            elif action in {"approval", "approval_required"}:
                self.control_mode = HumanControlMode.APPROVAL_REQUIRED
                accepted = True
            elif action == "approve":
                accepted = bool(approval_token)
                if accepted:
                    self.emit("human_approval_granted", {"actor": actor, "token": approval_token})
            elif action == "auto":
                self.control_mode = HumanControlMode.AUTO
                accepted = True
            else:
                accepted = False
            self.emit(
                "human_control_requested",
                {"action": action, "actor": actor, "reason": reason, "accepted": accepted},
                status="ok" if accepted else "rejected",
            )
            return accepted

    def reserve(
        self,
        costs: dict[str, float],
        *,
        reservation_id: str,
        actor: str = "runtime",
    ) -> BudgetDecision:
        with self._lock:
            if reservation_id in self._reservations:
                return BudgetDecision(True, "idempotent", reservation_id, self.remaining())
            normalized = {key: max(0.0, float(value)) for key, value in costs.items()}
            for key, amount in normalized.items():
                limit = self._limits.get(key, 0.0)
                if limit > 0 and self._used.get(key, 0.0) + amount > limit:
                    self.emit(
                        "budget_backpressure",
                        {"actor": actor, "resource": key, "requested": amount, "limit": limit},
                        status="blocked",
                    )
                    return BudgetDecision(
                        False,
                        f"{key}_budget_exhausted",
                        remaining=self.remaining(),
                    )
            for key, amount in normalized.items():
                self._used[key] = self._used.get(key, 0.0) + amount
            self._reservations[reservation_id] = {"actor": actor, "costs": normalized}
            self.emit("budget_reserved", {"reservation_id": reservation_id, "costs": normalized})
            return BudgetDecision(True, "reserved", reservation_id, self.remaining())

    def release(self, reservation_id: str, *, actual_costs: dict[str, float] | None = None) -> bool:
        with self._lock:
            reservation = self._reservations.pop(reservation_id, None)
            if reservation is None:
                return False
            reserved = reservation["costs"]
            actual = actual_costs or reserved
            for key, amount in reserved.items():
                self._used[key] = max(
                    0.0,
                    self._used.get(key, 0.0) - amount + max(0.0, float(actual.get(key, 0.0))),
                )
            self.emit("budget_released", {"reservation_id": reservation_id, "actual_costs": actual})
            return True

    def remaining(self) -> dict[str, float]:
        return {
            key: max(0.0, limit - self._used.get(key, 0.0)) if limit > 0 else float("inf")
            for key, limit in self._limits.items()
        }

    def retry(self, *, reason: str, attribution: FailureAttribution | None = None) -> bool:
        with self._lock:
            if self.terminal or self.attempt >= self.max_attempts:
                self.finish(
                    HarnessStatus.FAILED,
                    reason=HarnessFailureCode.RETRY_EXHAUSTED.value,
                    attribution=attribution,
                )
                return False
            self.attempt += 1
            self.failure = attribution
            self.status = HarnessStatus.RUNNING
            self.emit("retry_started", {"attempt": self.attempt, "reason": reason})
            return True

    def finish(
        self,
        status: HarnessStatus | str,
        *,
        reason: str = "",
        attribution: FailureAttribution | None = None,
    ) -> bool:
        target = HarnessStatus(str(status))
        with self._lock:
            if self.terminal:
                self.emit(
                    "run_terminal_late",
                    {"requested_status": target.value, "reason": reason},
                    status="late",
                    replayable=False,
                )
                return False
            if target not in _TERMINAL:
                return False
            if (
                target not in _ALLOWED_TRANSITIONS.get(self.status, set())
                and self.status != HarnessStatus.CREATED
            ):
                return False
            self.status = target
            self.stop_reason = reason
            self.failure = attribution
            self.metrics.finished_at = time.time()
            payload = {"reason": reason}
            if attribution is not None:
                payload["attribution"] = attribution.to_dict()
            self.emit("run_finished", payload, status=target.value)
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema_version": SCHEMA_VERSION,
                "run_id": self.run_id,
                "status": self.status.value,
                "attempt": self.attempt,
                "max_attempts": self.max_attempts,
                "phase": self.phase,
                "stop_reason": self.stop_reason,
                "failure": self.failure.to_dict() if self.failure else {},
                "control_mode": self.control_mode.value,
                "limits": dict(self._limits),
                "used": dict(self._used),
                "reservations": {key: dict(value) for key, value in self._reservations.items()},
                "events": [event.to_dict() for event in self._events],
                "metrics": self.metrics.snapshot(),
            }


def attribute_harness_failure(
    *,
    code: HarnessFailureCode | str,
    evidence_refs: list[str] | tuple[str, ...] | None = None,
    contributing_causes: list[str] | tuple[str, ...] | None = None,
    confidence: float = 0.85,
) -> FailureAttribution:
    """Create stable, evidence-backed attribution for reports and Bad Cases."""
    normalized = str(code)
    return FailureAttribution(
        primary_cause=normalized,
        contributing_causes=tuple(dict.fromkeys(str(item) for item in (contributing_causes or []))),
        confidence=max(0.0, min(1.0, float(confidence))),
        evidence_refs=tuple(dict.fromkeys(str(item) for item in (evidence_refs or []))),
    )


def stable_fingerprint(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _git_value(repo_root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def build_harness_manifest(
    repo_root: str | Path,
    *,
    run_id: str,
    base_commit: str = "",
    model: str = "",
    provider: str = "",
    prompt_hash: str = "",
    skill_hash: str = "",
    tool_schema_hash: str = "",
    config: dict[str, Any] | None = None,
    patch_hash: str = "",
) -> dict[str, Any]:
    """Build an immutable, replay-oriented manifest with a stable fingerprint."""
    root = Path(repo_root).resolve()
    core = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "base_commit": base_commit,
        "runtime_head": _git_value(root, "rev-parse", "HEAD"),
        "runtime_dirty": bool(
            _git_value(root, "status", "--porcelain=v1", "--untracked-files=all")
        ),
        "model": {"name": model, "provider": provider},
        "prompt_hash": prompt_hash,
        "skill_hash": skill_hash,
        "tool_schema_hash": tool_schema_hash,
        "patch_hash": patch_hash,
        "config": dict(config or {}),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "implementation": platform.python_implementation(),
            "cwd": str(root),
            "harness_engineering": SCHEMA_VERSION,
        },
    }
    manifest = dict(core)
    manifest["manifest_fingerprint"] = stable_fingerprint(core)
    return manifest


@dataclass
class BadCaseRecord:
    badcase_id: str
    run_id: str
    manifest_fingerprint: str
    primary_cause: str
    contributing_causes: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    trace_refs: list[str] = field(default_factory=list)
    status: str = "open"
    created_at: float = field(default_factory=time.time)
    closed_at: float = 0.0
    closure_reason: str = ""
    closure_run_id: str = ""

    def close(self, *, rerun_run_id: str, manifest_fingerprint: str, reason: str) -> bool:
        if self.status == "closed":
            return False
        if not rerun_run_id or manifest_fingerprint != self.manifest_fingerprint:
            return False
        self.status = "closed"
        self.closed_at = time.time()
        self.closure_reason = str(reason)
        self.closure_run_id = rerun_run_id
        return True

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class BadCaseStore:
    """Append-only JSONL store for Trace-to-Bad-Case feedback."""

    def __init__(self, repo_root: str | Path):
        self.path = Path(repo_root) / ".agent" / "badcases.jsonl"

    def append(self, record: BadCaseRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")

    def load(self) -> list[BadCaseRecord]:
        if not self.path.is_file():
            return []
        records: list[BadCaseRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                data = json.loads(line)
                records.append(BadCaseRecord(**data))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return records


def extract_bad_case(snapshot: dict[str, Any]) -> BadCaseRecord | None:
    """Convert a failed Harness snapshot into a reproducible Bad Case."""
    status = str(snapshot.get("status", ""))
    if status in {HarnessStatus.COMPLETED.value, "fixed"}:
        return None
    failure = snapshot.get("failure") or {}
    attribution = snapshot.get("attribution") or {}
    primary = str(
        failure.get("primary_cause")
        or attribution.get("primary_cause")
        or HarnessFailureCode.UNKNOWN.value
    )
    run_id = str(snapshot.get("run_id") or "")
    return BadCaseRecord(
        badcase_id="badcase-" + uuid.uuid4().hex[:16],
        run_id=run_id,
        manifest_fingerprint=str(snapshot.get("manifest_fingerprint") or ""),
        primary_cause=primary,
        contributing_causes=list(
            failure.get("contributing_causes")
            or attribution.get("contributing_causes")
            or []
        ),
        evidence_refs=list(failure.get("evidence_refs") or attribution.get("evidence_refs") or []),
        trace_refs=list(snapshot.get("trace_refs") or []),
    )


@dataclass(frozen=True)
class EvaluationPlan:
    manifest_fingerprint: str
    levels: tuple[str, ...] = tuple(level.value for level in EvaluationLevel)
    frozen: bool = True

    def gate(
        self,
        level: EvaluationLevel | str,
        *,
        manifest_fingerprint: str,
        passed: bool,
    ) -> dict[str, Any]:
        requested = EvaluationLevel(str(level))
        if not self.frozen or manifest_fingerprint != self.manifest_fingerprint:
            return {"allowed": False, "reason": "manifest_mismatch"}
        index = self.levels.index(requested.value) if requested.value in self.levels else -1
        if index < 0:
            return {"allowed": False, "reason": "level_not_in_plan"}
        return {
            "allowed": bool(passed),
            "reason": "passed" if passed else "prior_gate_failed",
            "level": requested.value,
        }


__all__ = [
    "BadCaseRecord",
    "BadCaseStore",
    "BudgetDecision",
    "EvaluationLevel",
    "EvaluationPlan",
    "ExecutionContract",
    "FailureAttribution",
    "HarnessControlPlane",
    "HarnessEvent",
    "HarnessFailureCode",
    "HarnessMetrics",
    "HarnessStatus",
    "HumanControlMode",
    "attribute_harness_failure",
    "build_harness_manifest",
    "extract_bad_case",
    "stable_fingerprint",
]
