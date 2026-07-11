"""Blackboard ↔ RepairState merge：Orchestrator 代理写入与物化。"""

from __future__ import annotations

from src.blackboard import Blackboard
from src.state import RepairState, RetrievedContext, SuspectLocation

SUSPECT_PREFIX = "suspect:"
CONTEXT_PREFIX = "context:"
LOCALIZER_SOURCE = "localizer"
RETRIEVER_SOURCE = "retriever"
BLACKBOARD_SCHEMA_VERSION = 1

_CONTEXT_FIELDS = (
    "related_tests",
    "similar_snippets",
    "caller_locations",
    "similar_fixes",
)


def suspect_key(suspect: SuspectLocation) -> str:
    """Blackboard key for a suspect location."""
    return f"{SUSPECT_PREFIX}{suspect.file_path}:{suspect.start_line}"


def context_key(field: str) -> str:
    """Blackboard key for a RetrievedContext field."""
    return f"{CONTEXT_PREFIX}{field}"


def write_localize_phase_to_blackboard(
    bb: Blackboard,
    suspects: list[SuspectLocation],
    context: RetrievedContext | None,
) -> dict:
    """Write parsed localize/retrieve outputs to the blackboard."""
    stats = {
        "suspects_written": 0,
        "context_keys_written": 0,
        "write_conflicts": 0,
    }
    for suspect in suspects:
        ok = bb.write(
            suspect_key(suspect),
            suspect.to_dict(),
            source_agent=LOCALIZER_SOURCE,
        )
        if ok:
            stats["suspects_written"] += 1
        else:
            stats["write_conflicts"] += 1

    ctx = context or RetrievedContext()
    for field in _CONTEXT_FIELDS:
        value = getattr(ctx, field, None)
        if not value:
            continue
        ok = bb.write(context_key(field), value, source_agent=RETRIEVER_SOURCE)
        if ok:
            stats["context_keys_written"] += 1
        else:
            stats["write_conflicts"] += 1
    return stats


def _dedupe_suspects(suspects: list[SuspectLocation]) -> list[SuspectLocation]:
    """Merge duplicate file+line suspects, keeping highest confidence."""
    by_location: dict[tuple[str, int], SuspectLocation] = {}
    for suspect in suspects:
        loc = (suspect.file_path, suspect.start_line)
        existing = by_location.get(loc)
        if existing is None or suspect.confidence > existing.confidence:
            by_location[loc] = suspect
    return sorted(
        by_location.values(),
        key=lambda s: (-s.confidence, s.file_path, s.start_line),
    )


def merge_blackboard_to_repair_state(
    state: RepairState,
    bb: Blackboard,
) -> dict:
    """Materialize blackboard entries into RepairState typed fields."""
    suspects: list[SuspectLocation] = []
    for _key, value in bb.read_related(SUSPECT_PREFIX).items():
        if isinstance(value, dict):
            suspects.append(SuspectLocation.from_dict(value))

    merged_suspects = _dedupe_suspects(suspects)

    context = RetrievedContext()
    for key, value in bb.read_related(CONTEXT_PREFIX).items():
        field = key[len(CONTEXT_PREFIX) :]
        if field in _CONTEXT_FIELDS and isinstance(value, list):
            setattr(context, field, list(value))

    state.suspect_locations = merged_suspects
    state.retrieved_context = context

    snapshot = bb.snapshot()
    state.blackboard_snapshot = snapshot

    return {
        "suspect_count": len(merged_suspects),
        "context_keys": len(bb.read_related(CONTEXT_PREFIX)),
        "conflicts": list(bb.conflicts),
        "snapshot": snapshot,
    }


def restore_blackboard_from_snapshot(bb: Blackboard, snapshot: dict | None) -> None:
    """Restore blackboard entries from a prior snapshot (checkpoint resume)."""
    if not snapshot:
        return
    for key, value in (snapshot.get("entries") or {}).items():
        bb.write(key, value, source_agent="restored")
