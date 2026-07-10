"""Standardized repair phase timings (Bonus6 §19.2).

Legacy top-level keys (``localizer_ms``, etc.) are dual-written for backward
compatibility. New readers should use ``phases.{phase}_ms`` only; legacy keys
are scheduled for removal after eval/CLI migration (target: post-Bonus6 PR).
"""

from __future__ import annotations

PHASES = ("localize", "retrieve", "patch", "verify", "repair_total")

LEGACY_MS_KEYS: dict[str, str] = {
    "localize": "localizer_ms",
    "retrieve": "retriever_ms",
    "patch": "patcher_ms",
    "verify": "verifier_ms",
}

LEGACY_INTERNAL_KEYS: dict[str, str] = {
    "localize": "localizer_internal",
    "retrieve": "retriever_internal",
    "patch": "patcher_internal",
    "verify": "verifier_internal",
}


def phase_ms_key(phase: str) -> str:
    """Canonical milliseconds key for a repair phase."""
    if phase == "repair_total":
        return "repair_total_ms"
    if phase not in LEGACY_MS_KEYS:
        raise ValueError(f"unknown phase: {phase}")
    return f"{phase}_ms"


def set_phase_ms(
    timings: dict,
    phase: str,
    ms: int,
    *,
    internal: dict | None = None,
) -> None:
    """Write canonical phase timing plus legacy alias keys."""
    key = phase_ms_key(phase)
    phases = timings.setdefault("phases", {})
    phases[key] = int(ms)
    legacy = LEGACY_MS_KEYS.get(phase)
    if legacy:
        timings[legacy] = int(ms)
    if internal is not None and phase in LEGACY_INTERNAL_KEYS:
        internal_key = LEGACY_INTERNAL_KEYS[phase]
        internal_copy = dict(internal)
        timings.setdefault("phases_internal", {})[phase] = internal_copy
        timings[internal_key] = internal_copy


def set_repair_total_ms(timings: dict, ms: int) -> None:
    """Write repair wall-clock total."""
    timings.setdefault("phases", {})["repair_total_ms"] = int(ms)


def set_parallel_wall_ms(timings: dict, localize_retrieve_ms: int) -> None:
    """Record L+R parallel wall time."""
    ms = int(localize_retrieve_ms)
    timings.setdefault("parallel_wall_ms", {})["localize_retrieve_ms"] = ms
    timings["localize_retrieve_ms"] = ms


def finalize_phases(timings: dict) -> dict:
    """Sync canonical ``phases`` with legacy top-level ms keys."""
    phases = timings.setdefault("phases", {})
    for phase, legacy_key in LEGACY_MS_KEYS.items():
        canon = phase_ms_key(phase)
        if canon not in phases and legacy_key in timings:
            phases[canon] = int(timings[legacy_key])
        elif legacy_key not in timings and canon in phases:
            timings[legacy_key] = int(phases[canon])
    total = phases.get("repair_total_ms")
    if total is not None:
        timings["repair_total_ms"] = int(total)
    return timings


def get_phase_ms(timings: dict, phase: str) -> int:
    """Read phase ms from canonical or legacy keys."""
    key = phase_ms_key(phase)
    phases = timings.get("phases") or {}
    if key in phases:
        return int(phases[key])
    if phase in LEGACY_MS_KEYS:
        legacy = LEGACY_MS_KEYS[phase]
        if legacy in timings:
            return int(timings[legacy])
    if key in timings:
        return int(timings[key])
    return 0


def phases_for_report(timings: dict) -> dict:
    """Return canonical phase block for report.json."""
    finalize_phases(timings)
    return dict(timings.get("phases") or {})
