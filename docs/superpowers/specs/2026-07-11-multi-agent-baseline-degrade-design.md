# Multi-Agent Baseline Degrade — Design Spec

**Date:** 2026-07-11  
**Status:** Implemented (Scheme A)  
**Refs:** `docs/bonus.md` §10.2 P2, §12.6  
**Branch:** `V1.2-Bonus3-limit-breaker-degrade`

## Problem

Multi-Agent repair exhausts `max_retries` on verify failures but terminates
with `exhausted` only. Eval has `SingleAgentOrchestrator` / baseline role, not
wired as a last-resort fallback in the main pipeline.

## Solution (Scheme A)

After the patch/verify retry loop in `_repair_impl`:

1. `should_degrade_to_baseline(state)` — verify enabled, retries exhausted,
   at least one verify failure, not cancel/timeout/fixed
2. `run_baseline_fallback(orch, state, initial_snapshot=...)` — restore clean
   workspace, create `baseline` Agent (shared `patcher.model_client`), `ask()`
3. `RepairState.degraded_mode = True` + trace `repair_degraded_to_baseline`
4. Reuse `apply_baseline_answer()` from `src/repair/baseline_apply.py`

## Trigger

Default: `retry_count >= max_retries` after at least one failed verify
(`verification_result` or `post_patch_pytest_code`).

## Deferred

- Early degrade after N verify fails (before exhausted)
- Baseline sandbox verify (match eval baseline skip-verify for now)
- CLI `--no-degrade`

## Acceptance

```bash
pytest tests/test_repair_degrade.py tests/test_baseline.py tests/test_orchestrator.py -v
```
