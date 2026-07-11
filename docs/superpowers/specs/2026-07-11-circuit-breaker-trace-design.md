# Circuit Breaker Trace Events — Design Spec

**Date:** 2026-07-11  
**Status:** Implemented (Scheme A)  
**Refs:** `docs/bonus.md` §10.1 P1  
**Branch:** `V1.2-Bonus3-limit-breaker-degrade`

## Problem

`CircuitBreaker` state machine existed but transitions were invisible in
`trace.jsonl`. Operators could only see `run_finished.stop_reason=circuit_breaker`
when OPEN rejected a call.

## Solution (Scheme A)

1. **`CircuitBreaker.add_listener(fn)`** — `fn(event, payload)` on state transitions
2. **`AgentLoop.run()`** registers `_circuit_trace_listener` → `_emit(event, payload)`;
   removed in `finally`
3. **`CircuitBreakerOpenError`** handled via `isinstance` in `_stop_for_api_error`

### Events

| Event | Trigger |
|-------|---------|
| `circuit_opened` | → OPEN (`consecutive_failures` or `half_open_probe_failed`) |
| `half_open_probe` | OPEN → HALF_OPEN (recovery timeout elapsed) |
| `circuit_closed` | HALF_OPEN → CLOSED |

OPEN fast-fail (no transition) → `run_finished.stop_reason=circuit_breaker` only.

## Deferred

- Native `chat_with_tools` path wrapped by `circuit_breaker.call`
- `report.json` circuit event aggregation

## Acceptance

```bash
pytest tests/test_circuit_breaker.py tests/test_agent_loop.py tests/test_wired_modules.py -v
```
