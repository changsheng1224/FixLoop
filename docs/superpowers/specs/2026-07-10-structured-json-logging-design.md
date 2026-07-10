# Structured JSON Logging — Design Spec

**Date:** 2026-07-10  
**Bonus ref:** `docs/bonus.md` §19.1 P1  
**Branch:** `V1.1-Bonus6-Observability`

## Goal

Enable machine-parseable stderr logs via `FIXLOOP_LOG=json`, reusing the existing `get_logger()` stack from §1.

## Decision

**Scheme A** — `JsonFormatter` on existing `StreamHandler(stderr)` + `ContextVar` correlation fields.

| Component | Role |
|-----------|------|
| `agent_runtime/log_context.py` | `run_id` / `agent` via contextvars |
| `agent_runtime/logging_setup.py` | `JsonFormatter`, `resolve_log_format()`, `FIXLOOP_LOG=json` |
| `agent_runtime/agent_loop.py` | `log_context()` for each `ask()` |
| `src/orchestrator.py` | `bind_run_id()` for repair session |

## JSON record schema

```json
{
  "ts": "2026-07-10T07:40:00.123Z",
  "level": "WARNING",
  "logger": "fixloop.orchestrator",
  "message": "[localizer] 0 suspects",
  "run_id": "550e8400-e29b-41d4-a716-446655440000",
  "agent": "localizer"
}
```

- `run_id` / `agent` omitted when unset (not `null`).
- Exceptions on ERROR+: `{type, message}`; full traceback only when log level is DEBUG.

## Non-goals

- Replace `trace.jsonl` / `CLIProgressCallback`
- `--log-format` CLI flag (env-only for this item)
- Run-directory `ops.log.jsonl`

## Acceptance

```bash
pytest tests/test_logging_setup.py -v
pytest tests/ -v
```
