"""One canonical decision envelope for legacy guidance and executable routing."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class CanonicalSkillDecision:
    selected: str | None
    selection_kind: str
    guidance_skill: str | None = None
    executable_skill: str | None = None
    executable_version: str | None = None
    fallback: bool = False
    reasons: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_canonical_skill_decision(matched, route) -> CanonicalSkillDecision:
    """Arbitrate both selectors into a single traceable decision object.

    Executable routing owns invocation. Guidance remains an attachment and
    cannot silently replace the executable capability.
    """
    guidance = getattr(matched, "name", None)
    executable = getattr(route, "selected", None)
    if executable:
        selected = executable
        kind = "executable"
    elif guidance:
        selected = guidance
        kind = "guidance"
    else:
        selected = None
        kind = "fallback"
    reasons = []
    if guidance:
        reasons.append("guidance_match")
    if executable:
        reasons.append(str(getattr(route, "selection_reason", "executable_route")))
    if not reasons:
        reasons.append("no_skill_admitted")
    return CanonicalSkillDecision(
        selected=selected,
        selection_kind=kind,
        guidance_skill=guidance,
        executable_skill=executable,
        executable_version=getattr(route, "skill_version", None),
        fallback=bool(getattr(route, "fallback", False) or selected is None),
        reasons=reasons,
        candidates=[
            {"name": item.name, "score": item.score, "tier": item.tier}
            for item in getattr(route, "candidates", [])
        ],
    )
