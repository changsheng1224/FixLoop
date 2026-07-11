# Tool Gateway Audit — Design Spec

**Date:** 2026-07-11  
**Status:** Implemented (Scheme A)  
**Refs:** `docs/bonus.md` §7.2 P2, §19.3 P2, `docs/bonus/DESIGN.md` §7.2  
**Branch:** `V1.2-Bonus2-tool-gateway`

## Problem

V1.1-Bonus5/6 wired `permission_denied` into trace, per-agent reports, and repair-level
`report.json` / `node_timings`, but **deferred** syncing gateway denials into
`RepairState.agent_errors` (see `2026-07-10-dual-rejection-semantics-design.md`).

Bonus §7.2 requires: `permission_denied` → trace / **agent_errors** / §19.3 metrics.

## Solution (Scheme A)

At repair finalize, after aggregating agent reports:

1. `summarize_repair_rejections(run_dir)` → `permission_denied_by_agent`
2. `apply_gateway_denials_to_agent_errors(state.agent_errors, by_agent)` writes:

   ```text
   localizer: gateway permission_denied: write_file×2
   ```

3. Existing parse/exception errors are preserved (`;` append).
4. Re-apply is idempotent (skips if gateway prefix already present).

### Unchanged (already implemented)

- Per-denial trace: `tool_executed` with `rejection_layer=gateway`
- Run summary: `repair_finished.gateway_denials`, `tool_rejection_metrics`
- Report: `permission_denied_by_tool`, `permission_denied_by_agent`

## Modules

| File | Change |
|------|--------|
| `src/repair/rejection_aggregate.py` | `format_gateway_denial_summary`, `apply_gateway_denials_to_agent_errors` |
| `src/orchestrator.py` | `_attach_rejection_stats` calls apply |

## Acceptance

```bash
pytest tests/test_rejection_aggregate.py tests/test_rejection_semantics.py -v
```

## Deferred

- Dedicated `permission_denied` trace event (B-lite; YAGNI)
- L1-only `ask` path agent_errors (repair is primary consumer)
