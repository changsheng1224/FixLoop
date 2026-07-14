# AgentLoop Finalizer Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move AgentLoop run finalization and report persistence out of `agent_runtime/agent_loop.py` without changing runtime behavior.

**Architecture:** Add `agent_runtime/loop_finalizer.py` with `finalize_agent_run(loop, ts)`. `AgentLoop._finalize_run()` becomes a small delegation point, while the new module owns report assembly, checkpoint/report persistence, durable memory promotion, candidate promotion, and session save.

**Tech Stack:** Python standard library, pytest, existing `RunStore`, `TaskState`, `checkpoint`, token accounting, and memory modules.

## Global Constraints

- Preserve Layer 1 boundaries: `agent_runtime/` must not import `src.*`.
- Preserve public API: `Agent.ask()`, `AgentLoop.run()`, report paths, checkpoint triggers, and report JSON keys must stay unchanged.
- Use TDD: add a failing test for the new finalizer module before production code.
- Run related tests before reporting completion.

---

### Task 1: Extract AgentLoop Finalizer

**Files:**
- Create: `agent_runtime/loop_finalizer.py`
- Modify: `agent_runtime/agent_loop.py`
- Test: `tests/test_loop_finalizer.py`

**Interfaces:**
- Consumes: `finalize_agent_run(loop, ts)`, where `loop` is an `AgentLoop` instance and `ts` is the current `TaskState`.
- Produces: unchanged report/checkpoint/session artifacts, with `AgentLoop._finalize_run()` delegating to the new function.

- [ ] **Step 1: Write the failing test**

```python
def test_agent_loop_delegates_finalize_to_loop_finalizer(monkeypatch, workspace):
    from agent_runtime.agent_loop import AgentLoop
    from agent_runtime.config import AgentConfig
    from agent_runtime.providers.clients import FakeModelClient
    from agent_runtime.runtime import Agent
    from agent_runtime.task_state import TaskState
    import agent_runtime.loop_finalizer as loop_finalizer

    agent = Agent(
        config=AgentConfig(provider="fake"),
        model_client=FakeModelClient(["<final>ok</final>"]),
        workspace=workspace,
    )
    loop = AgentLoop(agent)
    ts = TaskState.create(user_request="hello")
    calls = []

    monkeypatch.setattr(loop_finalizer, "finalize_agent_run", lambda actual_loop, actual_ts: calls.append((actual_loop, actual_ts)))

    loop._finalize_run(ts)

    assert calls == [(loop, ts)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -p no:cacheprovider tests/test_loop_finalizer.py::test_agent_loop_delegates_finalize_to_loop_finalizer -v`

Expected: FAIL because `agent_runtime.loop_finalizer` does not exist or `AgentLoop._finalize_run()` does not delegate.

- [ ] **Step 3: Implement minimal extraction**

Move the current `_finalize_run()` body into `agent_runtime/loop_finalizer.py` as `finalize_agent_run(loop, ts)`. Replace `self` references with `loop`. Move candidate promotion helper into the same module as `_promote_memory_candidates(agent, ts)`. Keep exception swallowing behavior unchanged.

- [ ] **Step 4: Run related tests**

Run: `pytest -p no:cacheprovider tests/test_loop_finalizer.py tests/test_agent_loop.py tests/test_wired_modules.py tests/test_bonus_features.py::TestTokenTracking::test_report_has_token_usage tests/test_rejection_semantics.py tests/test_stop_reasons.py tests/test_step_timeout.py -v`

Expected: PASS.

- [ ] **Step 5: Re-check Layer 1 boundary**

Run: `rg -n "from src\.|import src\." agent_runtime -g "*.py"`

Expected: no matches.
