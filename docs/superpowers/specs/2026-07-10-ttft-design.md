# TTFT / First-Byte Latency — Design Spec

**Date:** 2026-07-10  
**Bonus ref:** `docs/bonus.md` §19.4 P1  
**Branch:** `V1.1-Bonus6-Observability`

## Decision

Scheme A Phase 1: client-layer chunked HTTP read for TTFB, AgentLoop trace events,
per-agent report fields, repair-level aggregation.

## Per-agent report fields

- `ttft_ms_p50`, `ttft_ms_max`, `ttft_ms_last`
- `model_call_ms_total`
- `ttft_ms_by_call`: `[{step, attempt, ttft_ms, total_ms, output_tokens}]`

Omit all fields when no model calls occurred.

## Trace events

- `model_request_start` → `model_first_token` → `model_complete`

## Acceptance

```bash
pytest tests/test_model_timing.py tests/test_agent_loop.py tests/test_anthropic_client.py tests/test_ttft_aggregate.py -v
```
