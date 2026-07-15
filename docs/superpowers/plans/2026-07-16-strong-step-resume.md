# Strong Step Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade L1 Agent resume from session-level recovery to step-level recovery from the last successfully completed tool call.

**Architecture:** Add a durable step checkpoint record after each successful tool execution. On `--resume`, evaluate the latest checkpoint for workspace/runtime consistency and expose a resumable state that `AgentLoop` can consume before the next model turn.

**Tech Stack:** Python standard library, pytest, existing `AgentLoop`, `checkpoint`, `SessionStore`, `RunStore`, and `TaskState`.

## Global Constraints

- Do not restore Python call stacks or in-flight tool execution.
- Do not automatically replay `run_shell`; reuse the recorded observation when the checkpoint is valid.
- For write tools, allow exact resume only when affected file hashes still match the checkpoint post-state.
- Fall back to existing session resume when checkpoint validation is stale or incompatible.

---

### Task 1: Durable Step Checkpoint Payload

**Files:**
- Modify: `agent_runtime/checkpoint.py`
- Modify: `agent_runtime/agent_loop.py`
- Test: `tests/test_strong_step_resume.py`

**Interfaces:**
- Produces: `create_checkpoint(..., step_payload: dict | None = None)`
- Produces: checkpoint keys `resume_kind`, `step_index`, `tool`, `tool_args`, `tool_result`, `history_len`, `task_state`, `effects`

- [ ] **Step 1: Write failing tests** for tool step checkpoint payload and immediate session persistence.
- [ ] **Step 2: Run tests to verify failure** with missing payload fields.
- [ ] **Step 3: Extend checkpoint creation** and call it from `_run_tool_step`.
- [ ] **Step 4: Persist session/task state immediately** after successful tool step.
- [ ] **Step 5: Run focused tests** until green.

### Task 2: Resume Evaluation

**Files:**
- Modify: `agent_runtime/checkpoint.py`
- Test: `tests/test_strong_step_resume.py`

**Interfaces:**
- Produces: `evaluate_resume_state()` status `step-resumable` for valid step checkpoints.
- Produces: `resume_observation` metadata inside the result.

- [ ] **Step 1: Write failing tests** for `step-resumable`, stale file rejection, and identity mismatch fallback.
- [ ] **Step 2: Run tests to verify failure**.
- [ ] **Step 3: Validate post hashes for write effects** and runtime identity.
- [ ] **Step 4: Return structured resume metadata** for AgentLoop.
- [ ] **Step 5: Run focused tests** until green.

### Task 3: AgentLoop Re-entry

**Files:**
- Modify: `agent_runtime/runtime.py`
- Modify: `agent_runtime/agent_loop.py`
- Modify: `agent_runtime/cli.py`
- Test: `tests/test_strong_step_resume.py`

**Interfaces:**
- Consumes: `agent.session["resume_status"]` and `agent.session["resume_state"]`.
- Produces: AgentLoop behavior that injects the last tool observation and starts from the next model turn.

- [ ] **Step 1: Write failing test** that resumes after one completed tool and consumes only the final model output.
- [ ] **Step 2: Run test to verify failure**.
- [ ] **Step 3: Store resume metadata on Agent created from session**.
- [ ] **Step 4: Teach XML AgentLoop to consume the resume observation once**.
- [ ] **Step 5: Run focused tests** until green.

### Task 4: Regression Coverage

**Files:**
- Test: `tests/test_checkpoint_resume.py`
- Test: `tests/test_agent_loop.py`

**Interfaces:**
- Existing public API must remain compatible.

- [ ] **Step 1: Run existing checkpoint and agent loop tests**.
- [ ] **Step 2: Fix regressions without broad refactors**.
- [ ] **Step 3: Run final focused verification**.
