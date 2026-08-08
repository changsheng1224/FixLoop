"""Role-scoped projections for independent Critic and Verifier decisions."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

_COMMON_FIELDS = {
    "schema_version",
    "state_revision",
    "run_id",
    "phase",
    "status",
    "intent",
    "hypotheses",
    "evidence",
    "candidate_files",
    "verification",
    "verification_result",
    "active_roles",
}


def role_projection(state, role: str, *, input_revision: int | None = None) -> dict[str, Any]:
    """Return immutable public input; private model reasoning is excluded."""
    if hasattr(state, "to_dict"):
        raw = state.to_dict()
    else:
        raw = dict(state or {})
    projection = {key: deepcopy(raw[key]) for key in _COMMON_FIELDS if key in raw}
    projection["role"] = str(role)
    projection["input_revision"] = (
        int(input_revision)
        if input_revision is not None
        else int(raw.get("state_revision", 0) or 0)
    )
    if role == "critic":
        projection["candidate_patches"] = deepcopy(raw.get("candidate_patches") or [])
        projection["allowed_edit"] = list((raw.get("node_timings") or {}).get("allowed_edit") or [])
    elif role == "verifier":
        projection["candidate_patches"] = deepcopy(raw.get("candidate_patches") or [])
        projection["changed_files"] = list(raw.get("changed_files") or [])
        projection["verify_target"] = (raw.get("node_timings") or {}).get("verify_target", "")
    return projection


def validate_independent_input(projection: dict[str, Any], *, expected_role: str) -> list[str]:
    errors = []
    if projection.get("role") != expected_role:
        errors.append("role_mismatch")
    if not projection.get("input_revision", 0) >= 0:
        errors.append("invalid_input_revision")
    if expected_role in {"critic", "verifier"} and "candidate_patches" not in projection:
        errors.append("candidate_patches_required")
    return errors
