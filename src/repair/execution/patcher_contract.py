"""Patcher runtime contract and terminal status helpers.

This module keeps the repair loop focused on generic runtime guarantees:
evidence-aware prompts, explicit terminal states, and no-progress controls.
It must not encode dataset- or case-specific repair rules.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.state import CandidatePatch, RepairState

__all__ = [
    "PatcherPhase",
    "PatcherTerminalStatus",
    "PATCHER_TERMINAL_STATUSES",
    "classify_patcher_attempt",
    "begin_patcher_attempt",
    "derive_patcher_phase",
    "patcher_evidence_snapshot",
    "record_patcher_terminal_status",
    "terminal_status_from_answer",
    "render_patcher_runtime_contract",
]


class PatcherPhase(StrEnum):
    """Internal Patcher lifecycle; localization remains owned by Patcher."""

    LOCATING = "locating"
    GROUNDED = "grounded"
    READY_TO_PATCH = "ready_to_patch"
    PATCHING = "patching"
    TERMINAL = "terminal"


class PatcherTerminalStatus(StrEnum):
    PATCH_PRODUCED = "patch_produced"
    NEEDS_MORE_CONTEXT = "needs_more_context"
    CANNOT_PATCH = "cannot_patch"
    VERIFICATION_FAILED = "verification_failed"
    NO_PROGRESS = "no_progress"
    MODEL_OUTPUT_INVALID = "model_output_invalid"
    MODEL_OUTPUT_TRUNCATED = "model_output_truncated"
    CONTEXT_OVERFLOW = "context_overflow"
    EDIT_LOCK_BLOCKED = "edit_lock_blocked"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    LOCALIZATION_INCOMPLETE = "localization_incomplete"
    NO_CHANGE = "no_change"
    NO_WRITE_ATTEMPT = "no_write_attempt"
    WRITE_REJECTED = "write_rejected"
    PATCH_EXPORT_FAILED = "patch_export_failed"


# Statuses below describe a completed patcher attempt without a deliverable
# patch.  Keep this in one place so pipeline, failure tags, and benchmark
# reporting cannot silently drift apart as new terminal causes are added.
PATCHER_TERMINAL_STATUSES = frozenset(
    status.value
    for status in PatcherTerminalStatus
    if status is not PatcherTerminalStatus.PATCH_PRODUCED
)


def begin_patcher_attempt(state: RepairState) -> None:
    """Clear per-attempt markers while retaining the durable terminal history."""
    for key in (
        "patcher_terminal_status",
        "patcher_parse_failed",
        "patcher_apply_failed",
        "patcher_write_attempted",
        "patcher_write_rejected",
        "patcher_export_failed",
    ):
        state.control.reset(key)
    for key in (
        "patcher_terminal_reason",
        "patcher_phase",
        "patcher_evidence",
        "patch_no_change",
    ):
        state.node_timings.pop(key, None)
    for key in ("patcher_parse", "patcher_apply"):
        state.agent_errors.pop(key, None)


def patcher_evidence_snapshot(state) -> dict[str, int | bool]:
    """Summarize evidence without copying source text into the prompt."""
    context = getattr(state, "retrieved_context", None)
    suspects = list(getattr(state, "suspect_locations", None) or [])
    plan = getattr(state, "repair_plan", None)
    allowed = list(state.control.allowed_edit)
    tests = len(getattr(context, "related_tests", None) or []) if context else 0
    snippets = len(getattr(context, "similar_snippets", None) or []) if context else 0
    grounded = bool(suspects or allowed or (plan and getattr(plan, "suspect_files", None)))
    return {
        "suspects": len(suspects),
        "allowed_edit": len(allowed),
        "related_tests": tests,
        "similar_snippets": snippets,
        "grounded": grounded,
    }


def derive_patcher_phase(state, *, patches: list | None = None) -> PatcherPhase:
    """Derive the Patcher phase from durable state, not model prose."""
    if patches or getattr(state, "candidate_patches", None):
        return PatcherPhase.PATCHING
    status = state.control.patcher_terminal_status
    if status in PATCHER_TERMINAL_STATUSES:
        return PatcherPhase.TERMINAL
    evidence = patcher_evidence_snapshot(state)
    return PatcherPhase.GROUNDED if evidence["grounded"] else PatcherPhase.LOCATING


def terminal_status_from_answer(answer: str) -> PatcherTerminalStatus | None:
    """Parse an explicit model terminal declaration without treating it as patch JSON."""
    text = str(answer or "").strip()
    if not text:
        return None
    candidates = [text]
    final_match = re.search(r"<final>\s*(.*?)\s*</final>", text, flags=re.I | re.S)
    if final_match:
        candidates.insert(0, final_match.group(1).strip())
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            raw = str(payload.get("status") or payload.get("outcome") or "").lower()
            if raw in {
                "cannot_patch",
                "needs_more_context",
                "insufficient_evidence",
                "localization_incomplete",
            }:
                return PatcherTerminalStatus(raw)
    lowered = candidates[0].lower()
    for status in (
        PatcherTerminalStatus.CANNOT_PATCH,
        PatcherTerminalStatus.NEEDS_MORE_CONTEXT,
        PatcherTerminalStatus.INSUFFICIENT_EVIDENCE,
    ):
        if re.search(rf"(?<![a-z_]){re.escape(status.value)}(?![a-z_])", lowered):
            return status
    return None


def record_patcher_terminal_status(
    state: RepairState,
    status: str | PatcherTerminalStatus,
    *,
    reason: str = "",
    meta: dict | None = None,
) -> None:
    """Persist the latest patcher terminal status and append an audit event."""
    value = str(status.value if isinstance(status, PatcherTerminalStatus) else status)
    event = {
        "status": value,
        "reason": str(reason or ""),
        "retry_count": int(getattr(state, "retry_count", 0) or 0),
    }
    if meta:
        event["meta"] = dict(meta)
    state.control.patcher_terminal_status = value
    state.node_timings["patcher_terminal_reason"] = str(reason or "")
    history = state.node_timings.setdefault("patcher_terminal_history", [])
    if isinstance(history, list):
        history.append(event)
        del history[:-12]


def classify_patcher_attempt(
    state: RepairState,
    patches: list[CandidatePatch],
    *,
    apply_failed: bool = False,
    terminal_answer: str = "",
    agent_stop_reason: str = "",
) -> PatcherTerminalStatus:
    """Classify a patcher turn without deciding the concrete fix."""
    if patches:
        return PatcherTerminalStatus.PATCH_PRODUCED
    explicit = terminal_status_from_answer(terminal_answer)
    if explicit is not None:
        return explicit
    if agent_stop_reason == "model_output_truncated":
        return PatcherTerminalStatus.MODEL_OUTPUT_TRUNCATED
    if agent_stop_reason == "context_overflow":
        return PatcherTerminalStatus.CONTEXT_OVERFLOW
    if state.node_timings.get("unread_write_reject_count"):
        return PatcherTerminalStatus.EDIT_LOCK_BLOCKED
    if state.node_timings.get("patch_no_change"):
        return PatcherTerminalStatus.NO_CHANGE
    if state.control.patcher_write_rejected:
        return PatcherTerminalStatus.WRITE_REJECTED
    if state.control.patcher_export_failed:
        return PatcherTerminalStatus.PATCH_EXPORT_FAILED
    if state.control.patcher_write_attempted is False:
        return PatcherTerminalStatus.NO_WRITE_ATTEMPT
    if state.control.no_progress_warning:
        return PatcherTerminalStatus.NO_PROGRESS
    if apply_failed or state.agent_errors.get("patcher_apply"):
        return PatcherTerminalStatus.CANNOT_PATCH
    if state.agent_errors.get("patcher_parse") or state.control.patcher_parse_failed:
        return PatcherTerminalStatus.MODEL_OUTPUT_INVALID
    if not patcher_evidence_snapshot(state)["grounded"]:
        return PatcherTerminalStatus.LOCALIZATION_INCOMPLETE
    return PatcherTerminalStatus.NEEDS_MORE_CONTEXT


def _render_structured_feedback_hint(payload: dict) -> list[str]:
    lines: list[str] = []
    if not isinstance(payload, dict):
        return lines
    bucket = str(payload.get("bucket") or "")
    target = str(payload.get("verify_target") or "")
    action = str(payload.get("next_action") or "")
    if bucket or target or action:
        lines.append("[VERIFY FEEDBACK CONTRACT]")
    if bucket:
        lines.append(f"- bucket: {bucket}")
    if target:
        lines.append(f"- verify_target: {target}")
    if action:
        lines.append(f"- required_next_action: {action}")
    tests = payload.get("failing_tests") or []
    if tests:
        lines.append("- failing_tests:")
        for item in list(tests)[:4]:
            lines.append(f"  - {item}")
    files = payload.get("patch_files") or []
    if files:
        lines.append("- previous_patch_files: " + ", ".join(str(x) for x in files[:6]))
    return lines


def render_patcher_runtime_contract(state: RepairState | None) -> str:
    """Render generic runtime controls for the patcher prompt."""
    if state is None:
        return ""
    evidence = patcher_evidence_snapshot(state)
    phase = derive_patcher_phase(state)
    state.node_timings["patcher_phase"] = phase.value
    state.node_timings["patcher_evidence"] = dict(evidence)
    lines = [
        "[PATCHER RUNTIME CONTRACT]",
        f"- internal_phase: {phase.value}",
        "- evidence: " + ", ".join(f"{key}={value}" for key, value in evidence.items()),
        "- Decide by public issue, current source, tool results, evidence ledger, "
        "and verifier feedback only.",
        "- Do not use gold patches, gold test patches, dataset IDs, or case-specific shortcuts.",
        "- End each turn by producing a patch, asking for specific missing context, "
        "or declaring cannot_patch with evidence.",
        "- Prefer apply_patch with grounded pre-image; avoid repeating a previously rejected diff.",
    ]
    if phase == PatcherPhase.LOCATING:
        lines.extend(
            [
                "- You own localization. Use search/read/AST tools to find the "
                "implementation and failing expectation.",
                "- Do not write until a real implementation path and supporting "
                "source evidence are identified.",
                "- If evidence remains insufficient, call finish_repair with "
                "needs_more_context and name the missing evidence.",
            ]
        )
    elif phase == PatcherPhase.GROUNDED:
        lines.append(
            "- Localization evidence exists; make the smallest grounded implementation change now."
        )
    feedback_payload = state.control.structured_verify_feedback
    if isinstance(feedback_payload, dict):
        lines.extend(_render_structured_feedback_hint(feedback_payload))
    no_progress = state.control.no_progress_warning
    if isinstance(no_progress, dict) and no_progress:
        lines.append("[NO PROGRESS CONTROL]")
        lines.append(f"- no_progress_count: {no_progress.get('no_progress_count')}")
        lines.append(f"- required_next_action: {no_progress.get('required_next_action')}")
        if no_progress.get("forbid_repeated_reads"):
            lines.append("- repeated reads are disallowed unless they expand evidence.")
        allowed = no_progress.get("allowed_next_actions") or []
        if allowed:
            lines.append("- allowed_next_actions: " + ", ".join(str(x) for x in allowed))
    return "\n".join(lines)
