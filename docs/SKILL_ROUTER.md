# Skill Registry 与 Router（实现链路）

> 可执行 Skill 三件套 + 分级路由。与既有 YAML **策略 Skill**（prompt 注入）并存。  
> **代码**：`src/skills/registry.py`、`router.py`、`executable/`。计划见 `docs/2026-08-03-to-08-09-enhancement-plan.md`（8 月 6 日｜功能4）。

---

## 1. 边界

| 层 | 作用 |
|----|------|
| 策略 Skill（既有） | YAML + `match_skill` → Patcher/Localizer 提示 |
| 可执行 Skill（本切片） | Registry + Router + Runner → 结构化产物 |

本切片**不改** 11 个策略 YAML 的匹配语义。

---

## 2. 可执行 Skill（7）

| name | 输入 | 输出 | 工具 |
|------|------|------|------|
| `github_issue_ingestion` | Issue URL / #N | `issue_spec` | `github_get_issue`（可注入） |
| `stacktrace_localization` | traceback 文本 | `localization` | `stack_parse` |
| `regression_test_selection` | diff / 变更文件 | `selection` | `find_test`（可注入） |
| `repo_code_search` | 查询串 | `search` | `grep` / `read_file` |
| `baseline_verify` | 修前验证意图 | `baseline` | `run_tests` |
| `patch_apply_check` | diff / apply 意图 | `apply` | `patch_file` |
| `draft_pr_prepare` | PR 元数据意图 | `draft_pr`（强制 draft） | `github_create_draft_pr` |

规格：`src/skills/executable/specs.yaml`。  
评测：
- `router_cases.yaml`（易）+ `router_cases_hard.yaml`（可分难例）→ CI 门槛  
- `router_cases_heldout.yaml`（同义/口语/诱饵/双意图/弱信号）→ **仅诊断**，默认不进门槛，**不为刷分改 Router**

---

## 3. Registry / Router

```text
text
  │
  ▼
SkillRouter.score_candidates
  ├─ negative trigger → excluded
  ├─ positive trigger → rule（+ 关键词并列打破）
  ├─ keyword overlap
  └─ optional embed_fn
  │
  ▼
margin = s1 − s2
  ├─ rule_short_circuit / top1_margin → selected
  ├─ low margin + llm_pick_fn → llm_fallback
  └─ else → fallback（selected=null）
```

Trace 事件：`skill_routed`（`candidates` / `selection_reason` / `margin` / `skill_version` / `router_version`）。

Repair `parse_issue` 在 `skill_matched` 之后 fail-soft 发射。

---

## 4. 离线评测

```bash
# CI 门槛：easy + hard
python -c "from src.skills.router_eval import evaluate_router; print(evaluate_router().to_dict())"

# 诊断：held-out（不为刷分改 Router）
python -c "from src.skills.router_eval import evaluate_router, load_heldout_cases; print(evaluate_router(load_heldout_cases()).to_dict())"

pytest tests/test_skill_registry_router.py -v
```

评测集：
- `router_cases.yaml` + `router_cases_hard.yaml` → 门槛  
- `router_cases_heldout.yaml` → 诊断（同义/口语/诱饵/双意图/弱信号/中英夹杂）

指标：Top-1、误触发、漏触发、Fallback、低 Margin、Skill 切换。

---

## 5. 一句话

`src/skills` 可执行层：3 Skills + Registry + 规则/关键词/Embedding Router（低 Margin 可选 LLM）+ 离线路由评测与 `skill_routed` Trace。
