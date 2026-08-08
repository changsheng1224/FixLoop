"""Conservative usage feedback for routed and executed Skills."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class SkillUsageStage(StrEnum):
    ROUTED = "routed"
    PROJECTED = "projected"
    INVOKED = "invoked"
    OUTPUT_APPLIED = "output_applied"
    VERIFIED = "verified"


class SkillUsageOutcome(StrEnum):
    HELPFUL = "helpful"
    SUPPORTED = "supported"
    UNUSED = "unused"
    CONTRADICTED = "contradicted"
    HARMFUL = "harmful"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class SkillUsageEvent:
    skill_name: str
    skill_version: str
    stage: str
    outcome: str = SkillUsageOutcome.INCONCLUSIVE.value
    invocation_id: str = ""
    trace_id: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SkillFeedbackLedger:
    def __init__(self, state: dict[str, Any], *, trace=None) -> None:
        self.state = state
        self.trace = trace
        self.events = state.setdefault("skill_usage_events", [])

    def record(self, event: SkillUsageEvent) -> dict[str, Any]:
        if event.outcome in {
            SkillUsageOutcome.HELPFUL.value,
            SkillUsageOutcome.SUPPORTED.value,
            SkillUsageOutcome.CONTRADICTED.value,
            SkillUsageOutcome.HARMFUL.value,
        } and not event.evidence_refs:
            event = SkillUsageEvent(
                skill_name=event.skill_name,
                skill_version=event.skill_version,
                stage=event.stage,
                invocation_id=event.invocation_id,
                trace_id=event.trace_id,
                outcome=SkillUsageOutcome.INCONCLUSIVE.value,
            )
        raw = event.to_dict()
        self.events.append(raw)
        self.events[:] = self.events[-500:]
        if self.trace is not None:
            self.trace("skill_feedback_recorded", raw, "ok")
        return raw

    def summarize(self, skill_name: str) -> dict[str, int]:
        summary: dict[str, int] = {}
        for event in self.events:
            if event.get("skill_name") != skill_name:
                continue
            outcome = str(event.get("outcome", SkillUsageOutcome.INCONCLUSIVE.value))
            summary[outcome] = summary.get(outcome, 0) + 1
        return summary

    def record_stage(
        self,
        *,
        skill_name: str,
        skill_version: str,
        stage: SkillUsageStage,
        invocation_id: str = "",
        outcome: SkillUsageOutcome = SkillUsageOutcome.INCONCLUSIVE,
        evidence_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.record(
            SkillUsageEvent(
                skill_name=skill_name,
                skill_version=skill_version,
                stage=stage.value,
                invocation_id=invocation_id,
                outcome=outcome.value,
                evidence_refs=list(evidence_refs or []),
            )
        )

    def record_verification(
        self,
        *,
        skill_name: str,
        skill_version: str,
        invocation_id: str,
        output_applied: bool,
        verification_passed: bool | None,
        evidence_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Attribute only an applied output with explicit verification evidence."""
        evidence = list(evidence_refs or [])
        if not output_applied:
            outcome = SkillUsageOutcome.UNUSED
        else:
            self.record_stage(
                skill_name=skill_name,
                skill_version=skill_version,
                stage=SkillUsageStage.OUTPUT_APPLIED,
                invocation_id=invocation_id,
                evidence_refs=evidence,
            )
            if verification_passed is True and evidence:
                outcome = SkillUsageOutcome.SUPPORTED
            elif verification_passed is False and evidence:
                outcome = SkillUsageOutcome.CONTRADICTED
            else:
                outcome = SkillUsageOutcome.INCONCLUSIVE
        return self.record_stage(
            skill_name=skill_name,
            skill_version=skill_version,
            stage=SkillUsageStage.VERIFIED,
            invocation_id=invocation_id,
            outcome=outcome,
            evidence_refs=evidence,
        )
