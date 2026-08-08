"""Blackboard ↔ RepairState merge：Orchestrator 代理写入与物化。"""

from __future__ import annotations

from src.blackboard import BLACKBOARD_SCHEMA_VERSION as _BLACKBOARD_SCHEMA_VERSION
from src.blackboard import Blackboard
from src.state import RepairState, RetrievedContext, SuspectLocation

BLACKBOARD_SCHEMA_VERSION = _BLACKBOARD_SCHEMA_VERSION

SUSPECT_PREFIX = "suspect:"
CONTEXT_PREFIX = "context:"
SCRATCH_PREFIX = "scratch:"
RULE_SEED_SOURCE = "rule_seed"
RUNTIME_CONTEXT_SOURCE = "runtime_context"
ORCHESTRATOR_SOURCE = "orchestrator"
_CONTEXT_FIELDS = (
    "related_tests",
    "similar_snippets",
    "caller_locations",
    "similar_fixes",
)


def dedupe_suspects(suspects: list[SuspectLocation]) -> list[SuspectLocation]:
    """按 (file_path, start_line) 去重，保留最高置信度。"""
    seen: dict[tuple, SuspectLocation] = {}
    for s in suspects:
        key = (s.file_path, s.start_line)
        if key not in seen or s.confidence > seen[key].confidence:
            seen[key] = s
    return list(seen.values())


def suspect_key(suspect: SuspectLocation) -> str:
    """Blackboard key for a suspect location."""
    return f"{SUSPECT_PREFIX}{suspect.file_path}:{suspect.start_line}"


def context_key(field: str) -> str:
    """Blackboard key for a RetrievedContext field."""
    return f"{CONTEXT_PREFIX}{field}"


def scratch_key(field: str) -> str:
    """Blackboard key for scratch / TTL entries."""
    return f"{SCRATCH_PREFIX}{field}"


def write_seed_context_to_blackboard(
    bb: Blackboard,
    suspects: list[SuspectLocation],
    context: RetrievedContext | None,
) -> dict:
    """Write rule-seeded suspects and runtime context to the blackboard."""
    stats = {
        "suspects_written": 0,
        "context_keys_written": 0,
        "write_conflicts": 0,
    }
    for suspect in suspects:
        ok = bb.write(
            suspect_key(suspect),
            suspect.to_dict(),
            source_agent=RULE_SEED_SOURCE,
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
        ok = bb.write(context_key(field), value, source_agent=RUNTIME_CONTEXT_SOURCE)
        if ok:
            stats["context_keys_written"] += 1
        else:
            stats["write_conflicts"] += 1
    return stats


def write_feedback_to_blackboard(bb: Blackboard, feedback: str, *, ttl: float = 300) -> bool:
    """Write verify feedback to scratch namespace for next patch merge."""
    if not feedback:
        return False
    return bb.write(
        scratch_key("feedback"),
        feedback,
        source_agent=ORCHESTRATOR_SOURCE,
        ttl=ttl,
    )


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


def read_suspects_from_blackboard(bb: Blackboard) -> list[SuspectLocation]:
    """Read and dedupe suspects via ``read_related("suspect:")``."""
    suspects: list[SuspectLocation] = []
    for _key, value in bb.read_related(SUSPECT_PREFIX).items():
        if isinstance(value, dict):
            suspects.append(SuspectLocation.from_dict(value))
    return _dedupe_suspects(suspects)


def read_context_from_blackboard(bb: Blackboard) -> RetrievedContext:
    """Read retrieved context via ``read_related("context:")``."""
    context = RetrievedContext()
    for key, value in bb.read_related(CONTEXT_PREFIX).items():
        field = key[len(CONTEXT_PREFIX) :]
        if field in _CONTEXT_FIELDS and isinstance(value, list):
            setattr(context, field, list(value))
    return context


def _pick_conflict_winner(
    key: str,
    sources: list[str],
    values: list,
    strategy: str,
) -> tuple[str, object]:
    source_to_value = dict(zip(sources, values, strict=False))
    if strategy == "highest_confidence" and key.startswith(SUSPECT_PREFIX):
        best_source = sources[0]
        best_value = values[0]
        best_conf = -1.0
        for source, value in source_to_value.items():
            if not isinstance(value, dict):
                continue
            conf = float(value.get("confidence", 0) or 0)
            if conf > best_conf:
                best_conf = conf
                best_source = source
                best_value = value
        return best_source, best_value
    if strategy == "trusted_source_priority":
        priority = {
            "orchestrator": 100,
            "verifier": 90,
            "localizer": 80,
            "retriever": 70,
            "rule_seed": 60,
            "patcher": 50,
        }
        index = max(range(len(sources)), key=lambda i: priority.get(sources[i], 0))
        return sources[index], values[index]
    if strategy == "latest_valid":
        for source, value in reversed(list(source_to_value.items())):
            if value is not None:
                return source, value
    if strategy == "merge_list":
        merged: list = []
        for value in values:
            if isinstance(value, list):
                for item in value:
                    if item not in merged:
                        merged.append(item)
        if merged:
            return "merge", merged
    return sources[0], values[0]


def resolve_blackboard_conflicts(
    bb: Blackboard,
    *,
    strategy: str = "highest_confidence",
) -> list[dict]:
    """Arbitrate pending write conflicts and apply winners to the blackboard."""
    resolved: list[dict] = []
    for conflict in list(bb.conflicts):
        # Proposal/CAS conflicts do not have a legacy winner tuple.  Keep
        # them pending for an explicit retry or model-mediated decision.
        if conflict.get("status") in {"stale", "conflicted"}:
            continue
        key = conflict.get("key", "")
        sources = conflict.get("sources") or []
        values = conflict.get("values") or []
        if not key or not sources or len(sources) != len(values):
            # Rejected/CAS conflicts require a retry or explicit operator
            # action; they cannot be resolved by value arbitration.
            continue
        if strategy == "reject_all":
            bb.reject_conflict(key, reason="reject_all")
            resolved.append({"key": key, "strategy": strategy, "winner_source": ""})
            continue
        winner_source, winner_value = _pick_conflict_winner(key, sources, values, strategy)
        bb.apply_conflict_winner(key, winner_value, winner_source)
        resolved.append(
            {
                "key": key,
                "strategy": strategy,
                "winner_source": winner_source,
            }
        )
    return resolved


def _apply_scratch_feedback(state: RepairState, bb: Blackboard) -> bool:
    scratch_feedback = bb.read(scratch_key("feedback"))
    if isinstance(scratch_feedback, str) and scratch_feedback.strip():
        if not state.feedback:
            state.feedback = scratch_feedback
        return True
    return False


def merge_blackboard_for_patch(
    state: RepairState,
    bb: Blackboard,
    *,
    conflict_strategy: str = "highest_confidence",
) -> dict:
    """Merge blackboard into RepairState at patch boundary (read_related + resolve)."""
    conflicts_resolved = resolve_blackboard_conflicts(bb, strategy=conflict_strategy)
    suspects = read_suspects_from_blackboard(bb)
    context = read_context_from_blackboard(bb)

    state.suspect_locations = dedupe_suspects(suspects)
    state.retrieved_context = context
    scratch_applied = _apply_scratch_feedback(state, bb)

    snapshot = bb.snapshot()
    state.blackboard_snapshot = snapshot
    state.blackboard_revision = int(snapshot.get("revision", bb.revision) or 0)
    state.state_revision += 1
    state.collaboration_attribution = {
        "conflicts_resolved": len(conflicts_resolved),
        "conflicts_pending": len(bb.conflicts),
        "blackboard_revision": state.blackboard_revision,
    }

    return {
        "suspect_count": len(suspects),
        "context_keys": len(bb.read_related(CONTEXT_PREFIX)),
        "conflicts_resolved": conflicts_resolved,
        "conflicts": list(bb.conflicts),
        "scratch_feedback_applied": scratch_applied,
        "snapshot": snapshot,
        "retry_count": state.retry_count,
        "blackboard_revision": state.blackboard_revision,
    }


def restore_blackboard_from_snapshot(bb: Blackboard, snapshot: dict | None) -> None:
    """Restore blackboard entries from a prior snapshot (checkpoint resume)."""
    if not snapshot:
        return
    bb.restore_snapshot(snapshot)
