"""Canonical repair phase timings."""

from __future__ import annotations

PHASES = ("context", "patch", "verify", "repair_total")


def phase_ms_key(phase: str) -> str:
    """Canonical milliseconds key for a repair phase."""
    if phase == "repair_total":
        return "repair_total_ms"
    if phase not in PHASES:
        raise ValueError(f"unknown phase: {phase}")
    return f"{phase}_ms"


def set_phase_ms(
    timings: dict,
    phase: str,
    ms: int,
    *,
    internal: dict | None = None,
) -> None:
    """Write one canonical phase timing."""
    key = phase_ms_key(phase)
    phases = timings.setdefault("phases", {})
    phases[key] = int(ms)
    if internal is not None:
        timings.setdefault("phases_internal", {})[phase] = dict(internal)


def set_repair_total_ms(timings: dict, ms: int) -> None:
    """Write repair wall-clock total."""
    timings.setdefault("phases", {})["repair_total_ms"] = int(ms)


def finalize_phases(timings: dict) -> dict:
    """Ensure the canonical ``phases`` mapping exists."""
    timings.setdefault("phases", {})
    return timings


def get_phase_ms(timings: dict, phase: str) -> int:
    """Read a canonical phase duration."""
    key = phase_ms_key(phase)
    phases = timings.get("phases") or {}
    if key in phases:
        return int(phases[key])
    return 0


def phases_for_report(timings: dict) -> dict:
    """Return canonical phase block for report.json."""
    finalize_phases(timings)
    return dict(timings.get("phases") or {})
