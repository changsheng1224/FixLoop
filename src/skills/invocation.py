"""Skill invocation lifecycle and stable failure taxonomy."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class SkillInvocationStatus(StrEnum):
    SELECTED = "selected"
    ADMITTED = "admitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    SIDE_EFFECT_UNCERTAIN = "side_effect_uncertain"


class SkillErrorCode(StrEnum):
    SKILL_NOT_FOUND = "skill_not_found"
    VERSION_UNAVAILABLE = "version_unavailable"
    INPUT_INVALID = "input_invalid"
    PERMISSION_DENIED = "permission_denied"
    TOOL_BUDGET_EXHAUSTED = "tool_budget_exhausted"
    TOOL_FAILED = "tool_failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    OUTPUT_INVALID = "output_invalid"
    EVIDENCE_MISSING = "evidence_missing"
    SIDE_EFFECT_UNCERTAIN = "side_effect_uncertain"
    RUNNER_FAILED = "runner_failed"


@dataclass
class SkillInvocation:
    skill_name: str
    skill_version: str = ""
    invocation_id: str = field(default_factory=lambda: "SKI-" + uuid.uuid4().hex[:16])
    status: str = SkillInvocationStatus.SELECTED.value
    input_summary: str = ""
    input_hash: str = ""
    output_ref: str = ""
    side_effect_receipt: str = ""
    observation_id: str = ""
    error_code: str = ""
    error_message: str = ""
    admitted_tools: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    completion_evidence: list[str] = field(default_factory=list)
    side_effect_level: str = "none"
    idempotency_key: str = ""
    content_hash: str = ""
    fail_closed: bool = False
    retry_count: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0

    def transition(self, status: SkillInvocationStatus) -> None:
        self.status = status.value
        if status is SkillInvocationStatus.RUNNING and not self.started_at:
            self.started_at = time.time()
        if status in {
            SkillInvocationStatus.SUCCEEDED,
            SkillInvocationStatus.INCOMPLETE,
            SkillInvocationStatus.FAILED,
            SkillInvocationStatus.CANCELLED,
            SkillInvocationStatus.TIMED_OUT,
            SkillInvocationStatus.SIDE_EFFECT_UNCERTAIN,
        }:
            self.finished_at = time.time()

    def fail(self, status: SkillInvocationStatus, code: SkillErrorCode, message: str) -> None:
        self.error_code = code.value
        self.error_message = str(message)[:1000]
        self.transition(status)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SkillExecutionResult:
    invocation: SkillInvocation
    output: dict[str, Any] = field(default_factory=dict)
    observation: dict[str, Any] | None = None
    reused: bool = False

    @property
    def ok(self) -> bool:
        return self.invocation.status == SkillInvocationStatus.SUCCEEDED.value
