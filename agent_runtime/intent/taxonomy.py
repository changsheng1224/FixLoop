"""Review-gated governance for Intent taxonomy evolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agent_runtime.intent.models import INTENT_TAXONOMY_VERSION, PRIMARY_ACTIONS


class ProposalStatus(StrEnum):
    DISCOVERED = "discovered"
    EVIDENCE_READY = "evidence_ready"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


_TRANSITIONS = {
    ProposalStatus.DISCOVERED: {ProposalStatus.EVIDENCE_READY, ProposalStatus.REJECTED},
    ProposalStatus.EVIDENCE_READY: {ProposalStatus.REVIEWED, ProposalStatus.REJECTED},
    ProposalStatus.REVIEWED: {ProposalStatus.APPROVED, ProposalStatus.REJECTED},
    ProposalStatus.APPROVED: {ProposalStatus.ROLLED_BACK},
    ProposalStatus.REJECTED: set(),
    ProposalStatus.ROLLED_BACK: set(),
}


@dataclass
class TaxonomyProposal:
    proposal_id: str
    label: str
    merge_into: str | None = None
    status: str = ProposalStatus.DISCOVERED.value
    evidence_count: int = 0
    confirmed_count: int = 0
    confusion_rate: float = 1.0
    examples: list[str] = field(default_factory=list)
    reviewer: str = ""
    taxonomy_version: str = INTENT_TAXONOMY_VERSION
    rollback_to: str = ""

    def readiness(self, *, min_evidence: int = 20, min_confirmed: int = 5) -> dict[str, Any]:
        reasons = []
        if self.evidence_count < min_evidence:
            reasons.append("insufficient_evidence")
        if self.confirmed_count < min_confirmed:
            reasons.append("insufficient_confirmed_labels")
        if self.confusion_rate > 0.20:
            reasons.append("confusion_too_high")
        if self.label in PRIMARY_ACTIONS:
            reasons.append("label_already_exists")
        if self.merge_into and self.merge_into not in PRIMARY_ACTIONS:
            reasons.append("invalid_merge_target")
        return {"ready": not reasons, "reasons": reasons}

    def transition(self, target: str, *, reviewer: str = "") -> TaxonomyProposal:
        current = ProposalStatus(self.status)
        desired = ProposalStatus(target)
        if desired not in _TRANSITIONS[current]:
            raise ValueError(f"invalid taxonomy proposal transition: {current} -> {desired}")
        if desired == ProposalStatus.APPROVED:
            readiness = self.readiness()
            if not readiness["ready"]:
                raise ValueError(
                    "proposal is not evidence-ready: " + ",".join(readiness["reasons"])
                )
            if not reviewer:
                raise ValueError("approved proposal requires reviewer")
        self.status = desired.value
        if reviewer:
            self.reviewer = reviewer
        return self


def taxonomy_manifest() -> dict[str, Any]:
    return {
        "taxonomy_version": INTENT_TAXONOMY_VERSION,
        "labels": dict(PRIMARY_ACTIONS),
        "change_policy": "review_gated_no_auto_register",
    }


def proposal_from_candidate_card(card: Any, *, confusion_rate: float = 1.0) -> TaxonomyProposal:
    """Create a review proposal without mutating the closed taxonomy."""
    key = str(getattr(card, "key", "") or "candidate")
    label = str(getattr(card, "label_hint", "") or key).removeprefix("gap:")
    return TaxonomyProposal(
        proposal_id=f"intent:{key}",
        label=label,
        merge_into=getattr(card, "closest_existing", None),
        evidence_count=int(getattr(card, "count", 0) or 0),
        confirmed_count=int(getattr(card, "confirmed_count", 0) or 0),
        confusion_rate=max(0.0, min(1.0, float(confusion_rate))),
        examples=list(getattr(card, "example_texts", []) or [])[:8],
    )


__all__ = [
    "ProposalStatus",
    "TaxonomyProposal",
    "proposal_from_candidate_card",
    "taxonomy_manifest",
]
