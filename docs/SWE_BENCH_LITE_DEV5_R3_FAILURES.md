# SWE-bench Lite 开发集 R3 失败问题与证据

> 约束：`docs/SWE_BENCH_LITE_FIX_CONSTRAINTS.md`（改问题类，不特判题号）。  
> 前序：R1 `docs/SWE_BENCH_LITE_DEV5_FAILURES.md` §1–5；R2 同文档 §6。  
> **下一轮 R4（verify 开）**：`docs/SWE_BENCH_LITE_DEV5_R4_FAILURES.md`。  
> **R5**：`docs/SWE_BENCH_LITE_DEV5_R5_FAILURES.md`。  
> 本轮产物：`artifacts/swebench_lite_dev_live_r3/`  
> Agent：`artifacts/swebench_repos/<id>/.agent/runs|repairs/<run_id>/`  
> 记录日期：2026-08-05

---

## 0. 跑法与边界

```text
HF_HUB_OFFLINE=1
python -m src.benchmark.swebench
  --skip-clone --skip-verify
  --provider anthropic_compat --model-name fixloop-deepseek
  --output-dir artifacts/swebench_lite_dev_live_r3
  --work-root artifacts/swebench_repos
  --repair-timeout-s 900
```

| 项 | 值 |
|----|-----|
| Manifest | `skip_verify: true`，`run_harness: false` |
| 进程 | exit 1（`failure_summary.agent=5`）；报告 `ok: true` |
| 相对 R2 | 非空 patch **1→5**；**均未** FixLoop Verifier / 官方 harness |
| 代码时点 | 含 R2 类修（E2′/E10/E8/E3′/E6a′）；**未含**会话内后续 E11′（软校验 / retriever `max_steps=6`） |

日志：`artifacts/swebench_lite_dev_live_r3/patch_gen.log`

---

## 1. 五题总览

| instance_id | repair_status | patch_bytes | CR | verified | failure_detail | run_id |
|-------------|---------------|-------------|----|----------|----------------|--------|
| astropy__astropy-12907 | timeout | 212 | 0 | false | `patch_without_fixed_status` | `3b5b8837-0da9-4271-a1be-cc41f45df84b` |
| django__django-11099 | fixed | 57 | 0 | false | `unverified_patch` | `7d9f73b8-db69-4706-b2b4-3116990b383f` |
| matplotlib__matplotlib-23964 | fixed | 108 | 0 | false | `unverified_patch` | `4ffb85de-685b-4792-ae36-ec6d2bca802f` |
| pylint-dev__pylint-6506 | timeout | **627018** | 0 | false | `patch_without_fixed_status` | `7c7b4d92-c279-49b0-b779-8dd2981a7b9d` |
| sympy__sympy-20590 | timeout | **20286** | 0 | false | `patch_without_fixed_status` | `df2563f6-ea7e-4dc5-99d5-3ccbe94a0022` |

`adapter_report.json`：`none=0 / agent=5 / env=0 / eval=0`。

### repair_state 要点

| instance | status | failure_tags | agent_errors（摘要） | candidate_patches | retrieval_path |
|----------|--------|--------------|----------------------|-------------------|----------------|
| astropy | timeout | timeout | `phase timeout (patch, 90s…222.6s)` | 1 | `llm→degrade` |
| django | fixed | [] | {} | 1 | `llm→degrade` |
| matplotlib | fixed | [] | {} | 1 | `llm→degrade` |
| pylint | timeout | timeout | `hunk_mismatch:…config_initialization.py` + phase timeout 191s | **0** | `llm→degrade` |
| sympy | timeout | timeout | `patcher_parse=parse_fail` + phase timeout 160s | **0** | `llm→degrade` |

---

## 2. 相对 R2 的变化

| 观察 | R2（非 django 四题） | R3（五题） |
|------|---------------------|------------|
| nonempty | 1（仅 pylint 92B） | **5/5** |
| CR | pylint CR=0 | 全 CR=0 |
| E2′ 仓外路径拒写 | matplotlib 明显 | 本轮日志未见 `temp/...` 拒写 |
| apply / parse | sympy apply_failed；astropy parse_fail | pylint `hunk_mismatch`；sympy `parse_fail`；导出仍可能非空（见 E12） |
| schema 噪声 | 四题「不是 JSON 对象」 | 四题旧文案 + sympy `tool_call_not_final`（跑时未加载 E11′） |
| verify | skip | skip → django/mpl 标 `unverified_patch` |

---

## 3. 问题类卡片（R3 新/加重）

### E12 — 工作树脏 diff 污染导出（巨型 / 无关文件）

| 字段 | 内容 |
|------|------|
| class_id | `E12_dirty_worktree_export` |
| symptom | `candidate_patches=0` 或修复无关，但 `predictions` 仍导出数万～数十万字节；含大量无关路径 |
| affected_count | 2（pylint ~627KB / 697 个 `+++` 文件；sympy ~20KB / 6 个 shell 脚本） |
| fix_level | adapter export / worktree hygiene：仅导出本 run 触及文件；checkout 后强制 clean；拒绝超大/超多文件 patch |
| generic_rule | 导出不得等于「整仓 `git diff`」；timeout/apply_fail 时不得把历史脏状态当 model_patch |
| anti_overfit_check | 任意仓制造无关 CRLF/脚本改动后跑 export，不得进入 predictions |

**证据**

1. pylint `repair_state`：`candidate_patches=[]`，`patcher_apply=hunk_mismatch:pylint/config/config_initialization.py`，却 `predictions` **627018B**。  
2. patch 头样例：`doc/data/messages/l/logging-format-interpolation/details.rst` 等文档/fixture，**非**目标修复文件；`+++` 计数 **697**。  
3. sympy：`patcher_parse=parse_fail`，`cand=0`，导出含 `bin/test_sphinx.sh`、`release/*.sh` 等整文件替换。  
4. 对照：django/matplotlib `cand=1` 且体积小——说明「有候选」与「脏导出」可并存，E12 在失败路径上更致命。

```text
# patch_gen.log
[patcher] ⚠ 无法应用补丁: pylint/config/config_initialization.py
[patcher] 补丁解析成功但未写入任何文件
阶段超时: phase timeout (patch, 90s budget, consumed 191.2s)
[swebench] (4/5) done pylint-dev__pylint-6506 class=agent patch_bytes=626796
```

---

### E13 — 导出片段缺少 unified diff 头

| 字段 | 内容 |
|------|------|
| class_id | `E13_export_not_unified_diff` |
| symptom | `model_patch` 仅为 `+/-` 行片段，无 `---` / `+++` / `diff --git`，官方 harness 无法按文件 apply |
| affected_count | 3（astropy 212B、django 57B、matplotlib 108B） |
| fix_level | patch_export：始终产出带路径的 unified diff；或 harness 前校验失败则标 `env`/`agent` 并清空 |
| generic_rule | nonempty ≠ harness-ready；缺文件头视为无效导出 |
| anti_overfit_check | 合成只有 `+a\\n-b\\n` 的 payload → validate 失败 |

**证据**

```text
astropy  patch 起首: "-            separable_matrix = ..."
django   patch 起首: "-    regex = r'^[\\w.@+-]+$'"
matplotlib patch 起首: "-                for ps_name, xs_names in stream:"
# 三者 unified_header=False
```

路径：`artifacts/swebench_lite_dev_live_r3/predictions.jsonl`

---

### E14 — Patch 阶段 90s 预算超时（有/无落地）

| 字段 | 内容 |
|------|------|
| class_id | `E14_patch_phase_timeout` |
| symptom | `phase timeout (patch, 90s budget, consumed ≫90s)` → `repair_status=timeout`，即使已有候选或脏 diff |
| affected_count | 3（astropy 222.6s、pylint 191.2s、sympy 160.4s） |
| fix_level | phase_clock / patcher：超时后若已有合法 applied patch 应降级为可导出终态；或上调 SWE 默认 patch budget 并限 retry |
| generic_rule | timeout 与「是否已有可提交 patch」解耦记账 |
| anti_overfit_check | 合成：90s 内 apply 成功后强制超时 → 仍应导出该 patch 且 status 可区分 |

**证据**

```text
阶段超时: phase timeout (patch, 90s budget, consumed 222.6s)  # astropy
阶段超时: phase timeout (patch, 90s budget, consumed 191.2s)  # pylint
阶段超时: phase timeout (patch, 90s budget, consumed 160.4s)  # sympy
```

astropy：`cand=1` + timeout + `patch_without_fixed_status`（有片段导出但非 fixed）。

---

### E15 — skip_verify 下「fixed」仍标 agent / unverified

| 字段 | 内容 |
|------|------|
| class_id | `E15_unverified_fixed_as_agent` |
| symptom | `repair_status=fixed` 且 nonempty，但 `verified=false`、`failure_detail=unverified_patch`、计入 `agent` |
| affected_count | 2（django、matplotlib） |
| fix_level | adapter 归因：区分 `pending_verify` vs `agent`；或默认开 verify / 文档标明 skip_verify 指标口径 |
| generic_rule | 管道指标：`fixed_unverified` ≠ 模型失败 |
| anti_overfit_check | fake gold + skip_verify → 不得笼统打成能力失败 |

**证据**

- django / matplotlib：`repair_status=fixed`，`failure_class=agent`，`error=unverified_patch`。  
- Manifest：`extra.skip_verify=true`（主动跳过 Verifier）。

---

### E11″ — Retriever 仍未 submit，schema / degrade 噪声

| 字段 | 内容 |
|------|------|
| class_id | `E11_retriever_no_submit` |
| symptom | 日志 `schema 校验: 输出不是 JSON 对象` 或 `tool_call_not_final`；`retrieval_path=llm→degrade` |
| affected_count | 5（全员 degrade）；schema 文案 4 次旧式 + 1 次 `tool_call_not_final` |
| fix_level | 已部分落地 E11′（软校验、关 json_mode、`max_steps` 6）——**需用新代码复跑**验证；仍缺「探索后强制/提示 submit」与步数策略 |
| generic_rule | 无 `submit_retrieved_context` 成功则安静 degrade，不 WARNING「不是 JSON 对象」 |
| 状态 | 代码已改；**本 R3 进程未加载** |

**证据**

```text
schema 校验: 输出不是 JSON 对象   # ×4（astropy/django/mpl/pylint）
schema 校验: tool_call_not_final # sympy
# 五题 repair_state.retrieval_path == llm→degrade
```

astropy retriever：`stop_reason=step_limit`，`tool_steps=4`，`final_answer=""`（见该 run `task_state.retriever.json`）。

---

### E6a″ — apply / parse 失败与导出不一致

| 字段 | 内容 |
|------|------|
| class_id | `E6a_apply_parse_vs_export` |
| symptom | apply/parse 失败记入 `agent_errors`，但 predictions 仍 nonempty（常由 E12 放大） |
| affected_count | 2（pylint apply；sympy parse） |
| fix_level | export 闸门：无成功 `apply` 的 candidate → 禁止整仓 diff；`FailureTag.APPLY_FAILED` 已有，需与 export 联动 |
| generic_rule | `patch_bytes>0` 必须可追溯到本 run 的 applied patches |

**证据**

- pylint：`patcher_apply=hunk_mismatch:…`，`cand=0`，导出 627KB。  
- sympy：`patcher_parse=parse_fail`，`cand=0`，导出 20KB。

---

## 4. 残留 / 非本轮主因

| 类 | R3 观察 |
|----|---------|
| E1 CRLF in patch 文本 | 导出字符串 CR=0；但 E12 暗示**工作树**仍可能被换行污染 |
| E2′ 仓外相对路径 | 本轮日志未见典型 `temp/...` 拒写 |
| E3′ skill∩ACL | 未再作为本轮主证据采集 |
| E8 盘符拆分 | 未对本轮 `retrieved_context` 抽样（degrade 后多为 rule） |
| 官方 harness / Resolved | 未跑 |

---

## 5. 建议下一轮优先级

| 顺序 | class_id | 理由 | 状态（2026-08-05） |
|------|----------|------|---------------------|
| 1 | **E12** dirty export | 否则 nonempty 指标被噪声主导（pylint/sympy） | **已修**：无候选不整仓 diff；限文件/字节；reexport 同门禁 |
| 2 | **E13** unified diff 头 | 否则 django/mpl「fixed」也无法进 harness | **已修**：`original/patched` → `---`/`+++` unified |
| 3 | **E14** patch phase timeout | 有候选仍 timeout；与预算/终态策略相关 | **已修（归因）**：合法 patch + timeout → `timeout_with_patch`（none） |
| 4 | **E15** unverified 归因口径 | 避免 skip_verify 时误读「全 agent 失败」 | **已修**：`pending_verify`（none），harness 仍要 verified |
| 5 | E11′ 复跑确认 | 消 schema WARNING + 提高 submit 率 | 代码已有；待新代码复跑 |
| 6 | 开 verify 或 WSL harness | 对 E13 合格的小 patch（django/mpl）做 Resolved 探针 | 待办 |

单测：`tests/test_swebench_r3_export_gates.py` + `TestClassifyPostRepair`。

---

## 6. 产物索引

| 路径 | 用途 |
|------|------|
| `artifacts/swebench_lite_dev_live_r3/adapter_report.json` | 五题归因（内含完整 patch，pylint 极大） |
| `artifacts/swebench_lite_dev_live_r3/predictions.jsonl` | 导出 patch；CR=0；verified=false |
| `artifacts/swebench_lite_dev_live_r3/manifest.json` | `skip_verify=true` |
| `artifacts/swebench_lite_dev_live_r3/patch_gen.log` | schema / 超时 / apply 原文 |
| `artifacts/swebench_repos/<id>/.agent/repairs/<run_id>/repair_state.json` | status / errors / cand |
| `artifacts/swebench_repos/<id>/.agent/runs/<run_id>/` | trace / task_state / agent_report |

---

## 7. 纪律提醒

> Lite case 只提供失败形态样本；实现只修可迁移机制。若修复离开这 5 个 id 就失效，则视为过拟合。

改码前为拟修类补全 Failure Card（约束 §5）；合成夹具优先于真实 Lite 树断言。
