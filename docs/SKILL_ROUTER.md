# Skill Registry 与 Router（实现链路）

> 可执行 Skill 的注册、分级路由、Runner 与离线评测。与既有 YAML **策略 Skill**（prompt 注入）并存。  
> **代码权威**：`src/skills/registry.py`、`router.py`、`router_eval.py`、`executable/`。  
> **PR**：[#171](https://github.com/changsheng1224/FixLoop/pull/171)。计划勾选见 `docs/2026-08-03-to-08-09-enhancement-plan.md`（8 月 6 日｜功能4）。

---

## 1. 问题与边界

### 1.1 要解决什么

1. 把「可执行能力」从策略提示里拆出来：Issue 摄入、堆栈定位、选测、搜代码、基线、落补丁、Draft PR 等有稳定输入输出  
2. 用 **Registry** 管理规格（trigger / schema / tools / version / lifecycle）  
3. 用 **分级 Router**（规则 → 关键词/Embedding → 低 Margin 可选 LLM）选 Top-1，并把候选分、原因、版本写入 Trace  
4. 用分层评测集区分 **CI 门槛** 与 **held-out 诊断**，避免为刷分过拟合  

### 1.2 明确不做

- 不改写既有 11 个策略 YAML 的 `match_skill` / `skill_block` 语义  
- 不把 held-out 失败样例反向改 trigger 来刷 Top-1（诊断集默认不进门槛）  
- 不强制线上接 Embedding / LLM（二者均为可选注入）  
- Runner 默认不发起真实网络；GitHub / find_test 等可通过注入 Tool 接线  

### 1.3 在系统中的位置

```text
Orchestrator.parse_issue
        │
        ├─► resolve_skill_for_plan（策略 YAML）→ skill_matched / Gateway restrict
        │
        └─► SkillRouter.route(issue) ──fail-soft──► skill_routed
                    │
                    ▼（按需）
            execute_skill(name, args) → Governed Invocation + Observation
```

---

## 2. 能力全景

| 能力 | 说明 |
|------|------|
| ExecutableSkillSpec | Description、±Trigger、IO Schema、allowed_tools、evidence、version、lifecycle |
| SkillRegistry | `get/list/register/load_yaml/resolve_version`；默认加载 `executable/specs.yaml` |
| SkillRouter | 规则短路径 / Top-1 margin / LLM fallback / generic fallback |
| Runners | 7 个纯函数 Runner；公开调用经 Governed Execution Gateway，可注入 Tool |
| Trace | `skill_routed`：candidates、selection_reason、margin、skill_version、router_version |
| 离线评测 | easy / hard（门槛）+ heldout（诊断，含中文） |
| 指标 | Top-1、误触发、漏触发、Fallback、低 Margin、Skill 切换 |

---

## 3. 可执行 Skill（7）

| name | 职责 | 主要产物 | 典型工具 |
|------|------|----------|----------|
| `github_issue_ingestion` | Issue → IssueSpec | `issue_spec` | `github_get_issue` |
| `stacktrace_localization` | 错误栈 → 定位 | `localization` | `stack_parse` |
| `regression_test_selection` | Diff/变更 → 选测与 verify_scope | `selection` | `find_test` |
| `repo_code_search` | 符号/报错串搜索 | `search` | `grep` / `read_file` |
| `baseline_verify` | 修前基线验证计划 | `baseline` | `run_tests` |
| `patch_apply_check` | 应用 diff + 冒烟/静态检查计划 | `apply` | `patch_file` |
| `draft_pr_prepare` | Draft PR 元数据（**draft=true**） | `draft_pr` | `github_create_draft_pr` |

规格文件：`src/skills/executable/specs.yaml`。  
Runner：`src/skills/executable/__init__.py`（内部原语 `run_executable_skill` / 各
`run_*`）；公开入口为 `src.skills.execute_skill`。

计划首批三技能为前三者；后四者为同切片扩展，共用同一 Registry/Router。

---

## 4. Registry API（摘要）

```python
from src.skills.registry import SkillRegistry, get_default_executable_registry

reg = get_default_executable_registry()
reg.list(lifecycle="active")
reg.require("stacktrace_localization")
reg.resolve_version("draft_pr_prepare")  # name/version/lifecycle/fallback
```

---

## 5. Router 决策

```text
score_candidates(text)
  ├─ negative trigger → excluded
  ├─ positive trigger → rule（+ 关键词并列打破）
  ├─ keyword overlap（停用词过滤）
  └─ optional embed_fn
        │
        ▼
margin = s1 − s2
  ├─ 强 rule Top-1 → rule_short_circuit
  ├─ margin ≥ τ 且 score ≥ floor → top1_margin
  ├─ 低 margin + llm_pick_fn → llm_fallback
  └─ else → fallback（selected=null）
```

默认：`MARGIN_TAU=0.08`，`SCORE_FLOOR=0.45`，`ROUTER_VERSION=1`。

`RouteDecision.to_trace_payload()` 供 Trace 使用。  
Repair 侧：`src/repair/pipeline.py` 在 `skill_matched` 之后 fail-soft 发射 `skill_routed`。

Canonical 事件目录已含 `skill_routed`（见 `agent_runtime/canonical_trace.py` / `docs/CANONICAL_TRACE.md`）。

---

## 6. 离线评测

| 集 | 路径 | 用途 |
|----|------|------|
| easy | `src/skills/eval_cases/router_cases.yaml` | CI 门槛（含 `zh` 中英混合） |
| hard | `router_cases_hard.yaml` | CI 门槛（混淆/噪声/切换等） |
| heldout | `router_cases_heldout.yaml` | **仅诊断**；同义/口语/诱饵/弱信号/中文；**不为刷分改 Router** |

```bash
# 门槛
python -c "from src.skills.router_eval import evaluate_router; print(evaluate_router().to_dict())"

# 诊断 held-out
python -c "from src.skills.router_eval import evaluate_router, load_heldout_cases; print(evaluate_router(load_heldout_cases()).to_dict())"

pytest tests/test_skill_registry_router.py -v
```

指标字段：`top1`、`mis_trigger`、`miss_trigger`、`fallback_rate`、`low_margin_rate`、`skill_switch_rate`、`by_skill`、`by_tag`。

参考量级（合入时）：easy+hard ≈150 条门槛 Top-1≈1.0；heldout≈80 条 Top-1 明显更低（漏触发为主），属预期。

---

## 7. 与策略 Skill 的关系

| | 策略 Skill | 可执行 Skill |
|--|------------|--------------|
| 定义 | YAML catalog | `ExecutableSkillSpec` |
| 匹配 | `match_skill` / semantic | `SkillRouter` |
| Trace | `skill_matched`、`skill_hint_rendered` | `skill_routed` |
| 作用 | Prompt 提示 + suggested_tools | 结构化阶段产物 |

二者可同时存在于同一次 `parse_issue`。

---

## 8. 一句话总结

`src/skills` 可执行层：7 Skills + Registry + 规则/关键词/Embedding Router（低 Margin 可选 LLM）+ easy/hard/heldout 评测与 `skill_routed` Trace。

---

## 9. Governed Skill Runtime

Router selection is now separated from runtime admission and execution:

```text
Canonical Skill Decision
→ version / lifecycle / trust admission
→ input schema validation
→ runtime tool-policy intersection
→ timeout / cancellation / tool budget
→ runner
→ output schema / completion-evidence validation
→ Canonical Observation
→ usage feedback / Trace
```

Key modules:

| Module | Responsibility |
|---|---|
| `contract.py` | Canonical guidance/executable contract, SemVer, trust, scope and JSON contract validation |
| `decision.py` | One decision envelope over legacy guidance matching and executable routing |
| `execution.py` | Runtime-enforced admission and execution gateway |
| `invocation.py` | Invocation state machine and stable error taxonomy |
| `composition.py` | Bounded sequential composition, cancellation propagation and aggregate budgets |
| `feedback.py` | Conservative routed/projected/invoked/applied/verified usage feedback |
| `src/eval/skill_runtime_eval.py` | Execution-contract and outcome-ablation metrics |

Executable Skills cannot grant Tool permissions. Tool bindings must be declared by the Skill and
separately admitted by Runtime. Untrusted executable Skills are guidance-only. Side-effecting
Skills require an idempotency key or dry-run, and checkpoint resume pins the exact Skill version.
Local writes additionally require read-before-write evidence; declared preconditions and
postconditions are checked at the Gateway, and successful side effects produce a receipt.

Canonical Trace includes discovery, decision, admission, start, Tool call, completion, failure,
fallback and feedback events. Skill output is persisted as a provenance-bearing `OBS-*`
observation after central redaction.
