"""Patcher runtime contract and terminal status helpers.

This module keeps the repair loop focused on generic runtime guarantees:
evidence-aware prompts, explicit terminal states, and no-progress controls.
It must not encode dataset- or case-specific repair rules.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.state import CandidatePatch, RepairState

__all__ = [
    "PatcherTerminalStatus",
    "classify_patcher_attempt",
    "record_patcher_terminal_status",
    "render_patcher_runtime_contract",
]


class PatcherTerminalStatus(StrEnum):
    PATCH_PRODUCED = "patch_produced"
    NEEDS_MORE_CONTEXT = "needs_more_context"
    CANNOT_PATCH = "cannot_patch"
    VERIFICATION_FAILED = "verification_failed"
    NO_PROGRESS = "no_progress"
    MODEL_OUTPUT_INVALID = "model_output_invalid"


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
    state.node_timings["patcher_terminal_status"] = value
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
) -> PatcherTerminalStatus:
    """Classify a patcher turn without deciding the concrete fix."""
    if patches:
        return PatcherTerminalStatus.PATCH_PRODUCED
    if state.node_timings.get("no_progress_warning"):
        return PatcherTerminalStatus.NO_PROGRESS
    if apply_failed or state.agent_errors.get("patcher_apply"):
        return PatcherTerminalStatus.CANNOT_PATCH
    if state.agent_errors.get("patcher_parse") or state.node_timings.get(
        "patcher_parse_failed"
    ):
        return PatcherTerminalStatus.MODEL_OUTPUT_INVALID
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
    lines = [
        "[PATCHER RUNTIME CONTRACT]",
        "- Decide by public issue, current source, tool results, evidence ledger, "
        "and verifier feedback only.",
        "- Do not use gold patches, gold test patches, dataset IDs, or case-specific shortcuts.",
        "- End each turn by producing a patch, asking for specific missing context, "
        "or declaring cannot_patch with evidence.",
        "- Prefer apply_patch with grounded pre-image; avoid repeating a previously rejected diff.",
    ]
    feedback_payload = state.node_timings.get("structured_verify_feedback")
    if isinstance(feedback_payload, dict):
        lines.extend(_render_structured_feedback_hint(feedback_payload))
    no_progress = state.node_timings.get("no_progress_warning")
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
