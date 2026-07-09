# node_timings Standard Schema — Design Spec

**Date:** 2026-07-10  
**Bonus ref:** `docs/bonus.md` §19.2 P1  
**Branch:** `V1.1-Bonus6-Observability`

## Decision

Scheme A: canonical `phases` sub-object + legacy alias keys.

## Canonical keys (ms)

| Phase | Key |
|-------|-----|
| Localizer | `localize_ms` |
| Retriever | `retrieve_ms` |
| Patcher | `patch_ms` |
| Verifier | `verify_ms` |
| Full repair | `repair_total_ms` |

Legacy aliases (`localizer_ms`, etc.) remain writable for backward compatibility.

Retry semantics: **last attempt** for patch/verify (unchanged).

## Module

`src/repair/timing_schema.py` — `set_phase_ms`, `set_repair_total_ms`, `finalize_phases`, `get_phase_ms`, `phases_for_report`.

## Acceptance

```bash
pytest tests/test_timing_schema.py tests/test_orchestrator.py -v
```
