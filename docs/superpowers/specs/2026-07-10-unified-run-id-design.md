# Unified run_id (UUID) — Design Spec

**Date:** 2026-07-10  
**Bonus ref:** `docs/bonus.md` §19.1 P1  
**Branch:** `V1.1-Bonus6-Observability`

## Goal

L1 (`TaskState`) and L2 (`RepairRunTracer`) share one run_id generator: standard UUID v4 via stdlib.

## Decision

**Scheme A — pure UUID**, no `repair-` prefix.

| Component | Change |
|-----------|--------|
| `agent_runtime/run_ids.py` | `new_run_id()`, `is_valid_run_id()` |
| `TaskState.create()` | default `run_id=new_run_id()` |
| `RepairRunTracer.begin()` | `self.run_id = new_run_id()` |
| `AgentLoop._emit()` | `payload.setdefault("run_id", …)` |
| `RepairRunTracer.emit()` | `data.setdefault("run_id", self.run_id)` |

## Semantics

| Field | L1 ask | L2 repair |
|-------|--------|-----------|
| `run_id` | `new_run_id()` | Orchestrator-injected shared UUID |
| `task_id` | `run_id` | `{run_id}-{agent_name}` |
| `RepairState.repair_run_id` | — | same as shared `run_id` |

## Compatibility

- Existing run directories (timestamp / `repair-*`) remain readable; no migration.
- `--resume-repair` accepts any existing directory name.

## Acceptance

```bash
pytest tests/test_run_ids.py tests/test_persistence.py tests/test_orchestrator.py -v
```
