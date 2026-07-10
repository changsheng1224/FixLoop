# Gateway Rejection Count — Design Spec

**Date:** 2026-07-10  
**Bonus ref:** `docs/bonus.md` §19.3 P2  
**Branch:** `V1.1-Bonus6-Observability`

## Decision

Scheme A: aggregate `permission_denied_by_tool` (and related fields) from per-agent
`agent_report.*.json` into repair-level `report.json`.

## Repair report fields

- `permission_denied_by_tool`
- `tool_rejections_by_layer`
- `tool_rejections_by_gate`
- `permission_denied_by_agent`

Empty summaries omit fields (same as L1).

## Acceptance

```bash
pytest tests/test_rejection_aggregate.py tests/test_rejection_semantics.py -v
```
