# Intent Router Implementation Plan

> **For agentic workers:** Implement task-by-task with TDD. Steps use checkbox (`- [ ]`) syntax for tracking.  
> **Spec:** `docs/superpowers/specs/2026-08-03-intent-router-design.md`（**P0/P1 已完成；后续增强见该文档 §14–§18**）  
> **产品抽象：** `docs/INTENT_USER_RECOGNITION.md`  
> **Bonus / DESIGN:** `docs/bonus.md` §22 · `docs/bonus/DESIGN.md` §23
>
> **实现状态（2026-08）：** P0+P1 主路径已落地；另已实现 clarify-only、多轮指代、候选意图发现、  
> LLM 同次 candidates、分层/held-out 评测。新工作请以 spec §14–§18 为准，勿仅按下方未勾选 Task 理解现状。

**Goal:** Unified rule → embedding → (weak) LLM intent routing that produces an `IntentGraph` (multi-intent DAG, scheme A), with L2 repair folding and REPL topological serial execution.

**Architecture:** New package `agent_runtime/intent/` plans only (classify, slot, graph). P0 adapter folds repair channel to one `RepairPlan`. P1 thin `IntentGraphExecutor` runs executable nodes in topo order (fail-fast). No parallel DAG scheduler.

**Tech Stack:** Python stdlib (+ existing embed helpers), pytest, FakeClient for LLM, mock embeddings in CI.

## Global Constraints

- Action-level prototypes only (no issue_type embedding library).
- `issue_type` lives in `slots`, never as `primary`.
- Parallel edges may exist in representation; executor always serializes.
- Do not replace Skill YAML `trigger_pattern`.
- Preserve `_parse_issue` / `prompt_router` / Docker / Verifier semantics.
- CI **must** mock embedding (no network / model load in tests).

**Branch:** `bonus/intent-router`  
**Acceptance:**
- `pytest tests/test_intent_router.py tests/test_intent_embed.py tests/test_intent_graph.py -v`
- `pytest tests/test_orchestrator.py -v -k "ParseIssue or Inject or issue"`
- Full suite before PR: `pytest tests/ -v`

---

### Task 0: Plan + Spec on Branch

**Files:**
- Create: `docs/superpowers/plans/2026-08-03-intent-router.md` (this file)
- Keep: `docs/superpowers/specs/2026-08-03-intent-router-design.md`

- [x] **Step 1: Write this plan**
- [ ] **Step 2: Commit plan+spec with first code batch or separately**

---

### Task 1: Schema + Graph Utils

**Files:**
- Create: `agent_runtime/intent/__init__.py`
- Create: `agent_runtime/intent/models.py`
- Create: `agent_runtime/intent/graph.py`
- Test: `tests/test_intent_graph.py`

**Interfaces:**
- `IntentNode`, `IntentEdge`, `IntentGraph`, `IntentResult`, `RouteContext`, `Segment`
- `validate_graph(graph) -> IntentGraph | clarify`
- `topological_executable_nodes(graph) -> list[IntentNode]`
- `merge_constraints(graph) -> IntentGraph` (constrains → dst.slots; drop constraint nodes from exec set)
- Cycle / `max_executable_nodes` → clarify graph

- [ ] **Step 1: Write failing tests** for cycle, constrains merge, topo order (priority then span.start), >4 clarify
- [ ] **Step 2: Implement models + graph helpers**
- [ ] **Step 3: Run** `pytest tests/test_intent_graph.py -v` green

---

### Task 2: Segmenter

**Files:**
- Create: `agent_runtime/intent/segmenter.py`
- Test: `tests/test_intent_router.py` (segment section) or `tests/test_intent_segmenter.py`

**Interfaces:**
- `segment(text) -> list[Segment]` with `index`, `text`, `cue` (`None|sequential|additive`)
- Blank lines / CJK punct / `.?!` (protect `file.py`, `v1.2`) / cues `然后|另外|同时|and then|also`
- Fragments `< 2` chars merge into previous

- [ ] **Step 1: Failing tests** for multi-sentence, cue tags, short-fragment merge, traceback-ish lines stay coherent
- [ ] **Step 2: Implement segmenter**
- [ ] **Step 3: Focused tests green**

---

### Task 3: Rule Layer

**Files:**
- Create: `agent_runtime/intent/rules.py`
- Reuse: `SAVE_INTENT_WORDS` / `_has_save_intent` patterns from durable memory (import or thin shared check)

**Interfaces:**
- `RuleHit(primary, action, confidence, slots, reason)`
- `classify_rules(text, *, channel) -> RuleHit | None`
- Priority: slash (`/help` `/cancel`) → remember → strong bug/stack → short clarify → default ask
- `channel=repair` biases toward `repair_issue` + issue slots (files, issue_type hints)

- [ ] **Step 1: Failing tests** for remember, help, cancel, repair stack, ask default
- [ ] **Step 2: Implement rules**
- [ ] **Step 3: Focused tests green**

---

### Task 4: Embed Layer + Prototypes

**Files:**
- Create: `agent_runtime/intent/prototypes.yaml`
- Create: `agent_runtime/intent/embed_index.py`
- Test: `tests/test_intent_embed.py`

**Interfaces:**
- Load action-level prototypes (3–8 examples each)
- `EmbedIndex.match(text) -> (primary, score, margin) | None`
- Reuse semantic embed helpers when available; **injectable embed_fn** for tests
- Missing model → skip (return None)

- [ ] **Step 1: Failing tests** with mock embed_fn (top1/margin, unavailable skip)
- [ ] **Step 2: YAML + index**
- [ ] **Step 3: `pytest tests/test_intent_embed.py -v` green**

---

### Task 5: MultiIntent Planner

**Files:**
- Create: `agent_runtime/intent/planner.py`

**Interfaces:**
- `plan(segment_nodes, *, channel, max_executable=4) -> IntentGraph`
- Modes: `single` | `multi` | `hybrid`
- Constraint heuristics before second executable; ask+ask merge to single ask
- Edges: hybrid→`constrains`; multi→`sequence` chain; explicit 先/必须 → `depends_on`
- `cancel`/`help` exclusivity; repair channel force-fold to 1× repair executable

- [ ] **Step 1: Failing gold tests** hybrid / multi / ask-merge / repair fold
- [ ] **Step 2: Implement planner**
- [ ] **Step 3: Focused tests green**

---

### Task 6: LLM Fallback

**Files:**
- Create: `agent_runtime/intent/llm_fallback.py`

**Interfaces:**
- `maybe_refine_graph(graph, text, client, *, tau_llm=0.55) -> IntentGraph`
- JSON schema validation (closed primary/role/kind); illegal → keep rule graph or clarify
- No client / Fake skip path

- [ ] **Step 1: Failing tests** with FakeClient JSON refine + illegal fallback
- [ ] **Step 2: Implement**
- [ ] **Step 3: Focused tests green**

---

### Task 7: Router Orchestration

**Files:**
- Create: `agent_runtime/intent/router.py`
- Update: `agent_runtime/intent/__init__.py` exports
- Test: `tests/test_intent_router.py` gold suite

**Interfaces:**
- `IntentRouter.route(text, context: RouteContext) -> IntentResult`
- Pipeline: preprocess → segment → per-segment rule+embed fuse → planner → optional LLM → IntentResult
- Fuse: strong rule wins; agreement weighted; conflict in `raw_signals`
- `action=run_graph` only when `mode=multi` and ≥2 executables

Gold cases (spec §10):
- TypeError issue → single repair_issue
- `修 X。只改 a.py` → hybrid + constrains + file slot
- `记住用 pytest。然后修这个失败` → multi + sequence
- Pure multiline traceback → single repair
- Two ask sentences → merged single ask
- Synthetic cycle / >4 exec → clarify
- Embed conflict → rule priority; embed unavailable → still works

- [ ] **Step 1: Write gold failing tests**
- [ ] **Step 2: Implement router**
- [ ] **Step 3: `pytest tests/test_intent_router.py -v` green**

---

### Task 8: P0 L2 Wiring

**Files:**
- Create: `agent_runtime/intent/adapters/__init__.py`
- Create: `agent_runtime/intent/adapters/repair_plan.py`
- Modify: `src/orchestrator.py` (`_parse_issue` delegates adapter; keep classify/LLM issue_type logic inside or called by adapter)
- Trace: emit `intent_routed` with mode/nodes/edges summary

**Interfaces:**
- `IssueIntentAdapter.to_repair_plan(result, *, classify_fn...) -> RepairPlan`
- channel=repair: expect single/hybrid; if multi, keep highest-conf repair, `raw_signals.dropped_nodes`
- Still call `apply_prompt_routing(plan)`

- [ ] **Step 1: Failing orchestrator regression / adapter unit tests**
- [ ] **Step 2: Wire adapter + trace**
- [ ] **Step 3: `pytest tests/test_orchestrator.py -v -k "ParseIssue or Inject or issue"` green**

---

### Task 9: P1 REPL Serial Executor

**Files:**
- Create: `agent_runtime/intent/executor.py` (or under `agent_runtime/` near REPL)
- Modify: `agent_runtime/cli.py` / REPL entry to `route` then dispatch
- Test: serial remember→repair call order; clarify/help short-circuit; fail-fast

**Interfaces:**
- `IntentGraphExecutor.serial(result, handlers) -> list[step outcomes]`
- Topo sort executables only; merge constrains assert; fail-fast on handler error

- [ ] **Step 1: Failing call-order tests**
- [ ] **Step 2: Implement executor + thin REPL hook**
- [ ] **Step 3: Focused tests green**

---

### Task 10: Regression + PR

- [ ] **Step 1:** `pytest tests/test_intent_*.py tests/test_orchestrator.py -v` (scoped)
- [ ] **Step 2:** `pytest tests/ -v` full suite
- [ ] **Step 3:** Lint clean on touched files
- [ ] **Step 4:** Push + `gh pr create` (with proxy env)

---

## File Layout (target)

```text
agent_runtime/intent/
  __init__.py
  models.py
  segmenter.py
  rules.py
  embed_index.py
  prototypes.yaml
  llm_fallback.py
  planner.py
  graph.py
  router.py
  executor.py
  adapters/
    __init__.py
    repair_plan.py
tests/
  test_intent_graph.py
  test_intent_embed.py
  test_intent_router.py
```

## Threshold Defaults (spec §5.5)

| Name | Default |
|------|---------|
| `τ_node` | 0.55 |
| `τ_llm` | 0.55 |
| `τ_clarify` | 0.45 |
| `τ_exec` | 0.60 |
| `max_executable_nodes` | 4 |
