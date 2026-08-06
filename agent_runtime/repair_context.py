"""Structured, task-scoped repair state shared by context and memory layers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RepairContextState:
    goal: str = ""
    current_hypothesis: str = ""
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    confirmed_facts: list[dict[str, Any]] = field(default_factory=list)
    rejected_hypotheses: list[dict[str, Any]] = field(default_factory=list)
    candidate_files: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    edit_scope: list[str] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    next_action: str = ""
    blocked_reason: str = ""
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "current_hypothesis": self.current_hypothesis,
            "hypotheses": list(self.hypotheses[-8:]),
            "confirmed_facts": list(self.confirmed_facts[-12:]),
            "rejected_hypotheses": list(self.rejected_hypotheses[-8:]),
            "candidate_files": list(dict.fromkeys(self.candidate_files[-12:])),
            "changed_files": list(dict.fromkeys(self.changed_files[-12:])),
            "edit_scope": list(dict.fromkeys(self.edit_scope[-12:])),
            "verification": dict(self.verification),
            "next_action": self.next_action,
            "blocked_reason": self.blocked_reason,
            "evidence_refs": list(dict.fromkeys(self.evidence_refs[-20:])),
        }


def get_repair_context(memory: dict) -> dict[str, Any]:
    working = memory.setdefault("working", {})
    raw = working.setdefault("repair_context", {})
    if not isinstance(raw, dict):
        raw = {}
        working["repair_context"] = raw
    for key, value in RepairContextState().to_dict().items():
        raw.setdefault(key, value)
    return raw


def update_repair_context(memory: dict, **updates: Any) -> dict[str, Any]:
    state = get_repair_context(memory)
    for key, value in updates.items():
        if key not in RepairContextState.__dataclass_fields__:
            continue
        if isinstance(value, list):
            state[key] = list(value)
        elif isinstance(value, dict):
            state[key] = dict(value)
        else:
            state[key] = str(value or "")
    return state


def render_repair_context(memory: dict, *, max_chars: int = 3600) -> str:
    state = get_repair_context(memory)
    compact = {key: value for key, value in state.items() if value not in ("", [], {})}
    if not compact:
        return ""
    return "修复状态（当前任务事实优先；历史记忆仅作候选）:\n" + json.dumps(
        compact, ensure_ascii=False, separators=(",", ":")
    )[:max_chars]


def context_integrity(
    memory: dict,
    *,
    issue: str = "",
    projected_history: list[dict] | None = None,
) -> dict[str, Any]:
    state = get_repair_context(memory)
    history_text = "\n".join(str(item.get("content", "")) for item in projected_history or [])
    checks = {
        "goal": not state.get("goal") or bool(issue) or state["goal"][:80] in history_text,
        "hypothesis": not state.get("current_hypothesis")
        or state["current_hypothesis"][:80] in history_text,
        "verification": not state.get("verification") or "verif" in history_text.lower(),
        "next_action": not state.get("next_action")
        or state["next_action"][:80] in history_text,
    }
    return {"ok": all(checks.values()), "checks": checks}
