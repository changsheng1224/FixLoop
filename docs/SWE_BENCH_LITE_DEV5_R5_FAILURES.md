# SWE-bench Lite 开发集 R5 失败问题与证据

> 约束：`docs/SWE_BENCH_LITE_FIX_CONSTRAINTS.md`（改问题类，不特判题号）。  
> 前序：R1/R2 `docs/SWE_BENCH_LITE_DEV5_FAILURES.md`；R3 `docs/SWE_BENCH_LITE_DEV5_R3_FAILURES.md`；R4 `docs/SWE_BENCH_LITE_DEV5_R4_FAILURES.md`。  
> **下一轮 R6**：`docs/SWE_BENCH_LITE_DEV5_R6_FAILURES.md`。  
> 本轮产物：`artifacts/swebench_lite_dev_live_r5/`  
> Agent：`artifacts/swebench_repos/<id>/.agent/runs|repairs/<run_id>/`  
> 记录日期：2026-08-05

---

## 0. 跑法与边界

```text
HF_HUB_OFFLINE=1
HTTPS_PROXY/HTTP_PROXY=http://127.0.0.1:7897
python -m src.benchmark.swebench
  --provider anthropic_compat --model-name fixloop-deepseek
  --output-dir artifacts/swebench_lite_dev_live_r5
  --work-root artifacts/swebench_repos
  --repair-timeout-s 900
  --max-retries 1
  # 未传 --skip-verify → FixLoop verifier 开启
  # 未传 --skip-clone → checkout --force + clean base_commit
```

| 项 | 值 |
|----|-----|
| Manifest | `skip_verify: false`，`run_harness: false`，`allow_unverified_harness: false` |
| 进程 | exit 1；报告 `ok: true`；`failure_summary`: `none=0 / agent=5` |
| 代码时点 | R4→R5 根因修复后（E16 shell exec、E17 FAIL_TO_PASS 提示、E18 baseline schema retry、E6a feedback） |
| 相对 R4 | 非空 **1→2**（django、matplotlib）；E16 pip 恢复（`pip≈9s` + `pytest>0`）；仍 **0 verified** |

日志：`artifacts/swebench_lite_dev_live_r5/patch_gen.log`

---

## 1. 五题总览

| instance_id | repair_status | patch_B | verified | failure_detail | 主要 tags / errors | duration_ms | run_id |
|-------------|---------------|---------|----------|----------------|-------------------|-------------|--------|
| astropy__astropy-12907 | timeout | 0 | false | `empty_model_patch` | `timeout`；phase patch 90s 预算耗尽；cand=0；vr=None | 291565 | `6fb21d6f-…` |
| django__django-11099 | exhausted | **346** | false | `patch_without_fixed_status` | `verify_config`+`degraded_baseline`；sibling；FAIL_TO_PASS 非 pytest nodeid | 367949 | `1b962d9c-…` |
| matplotlib__matplotlib-23964 | exhausted | **259** | false | `patch_without_fixed_status` | `verify_config`+`degraded_baseline`；**lines 字段为 list-repr**；sibling | 212304 | `461e08d2-…` |
| pylint-dev__pylint-6506 | timeout | 0 | false | `empty_model_patch` | `timeout`；parse_fail；cand=0；vr=None | 432654 | `cc823a15-…` |
| sympy__sympy-20590 | exhausted | 0 | false | `empty_model_patch` | `apply_failed`；`hunk_mismatch:sympy/core/basic.py` | 250664 | `f8d9c5aa-…` |

共性：`retrieval_path=llm→degrade`（五题；`agent_incomplete` / `tool_call_not_final`）。

### 相对 R4 对照

| 现象 | R4 | R5 | 解读 |
|------|----|----|------|
| E16 pip/`mkdir --user` | django/mpl `pip≈110ms pytest=0` | django/mpl `pip≈9s pytest≈200–600ms` | **E16 根因已验证修好** |
| nonempty 导出 | 仅 sympy 267B | django 346B + matplotlib 259B | 能力/导出改善；仍未 verified |
| verify 空收集 | astropy/pylint「未收集到测试」 | django/mpl 标 `verify_config:`（有 target） | E17 有提示路径，但 **格式/可达性仍失败** |
| timeout 空导出 | sympy 有 patch→`timeout_with_patch`/`none` | astropy/pylint timeout 且 cand=0→`empty_model_patch`/`agent` | E14 归因仍正确：无合法候选则不伪装 |

结论：R5 证明管道闸门 + E16 沙箱修复有效；当前瓶颈转向 **verify 目标格式（E17′）**、**apply/sibling（E6a）**、**patch 字段规范化（E19）**、**patch 阶段预算（E14）**。

---

## 2. 问题类卡片（R5）

### E16′ — 沙箱 pip（回归验证：已通过）

| 字段 | 内容 |
|------|------|
| class_id | `E16_sandbox_pip_mkdir_user` |
| status | **closed in R5**（R4→R5 修复生效） |
| evidence | `patch_gen.log`：`pip=9066ms pytest=208ms`（django）、`pip=9551ms pytest=632ms`（matplotlib）；无 `mkdir: unrecognized option '--user'` |
| note | 仍可能有依赖装不全导致收集失败，但不再卡在 argv 拆分 |

---

### E17′ — FAIL_TO_PASS 提示无法被 pytest 收集

| 字段 | 内容 |
|------|------|
| class_id | `E17_fail_to_pass_not_pytest_nodeid` |
| symptom | `related_tests` / `test_path` 非空，但 `total_tests=0`；`failure_logs` 含 `verify_config: 未收集到任何测试 (target=…)`；tag=`verify_config` |
| affected_count | 2+（django、matplotlib；R4 同类形态在 astropy/pylint） |
| fix_level | FAIL_TO_PASS → pytest nodeid / 文件路径规范化；unittest 风格 `test_x (mod.Class)` 转 `path::Class::test_x`；路径存在性校验后才跑 |
| generic_rule | 任意 SWE 题的 FAIL_TO_PASS 字符串格式不一致；不得假设已是 pytest nodeid |
| anti_overfit_check | 合成 3 种格式（pytest / unittest / bare name）各 1 条 fixture，不绑 django |

**证据**

1. django `repair_state`：

   ```text
   related_tests[0]=
     test_ascii_validator (auth_tests.test_validators.UsernameValidatorsTests)
   vr_logs=
     verify_config: 未收集到任何测试
       (target=test_ascii_validator (auth_tests.test_validators.UsernameValidatorsTests))
   tags=['verify_config', 'degraded_baseline']
   cand=1；baseline_parse_recovered=True
   ```

2. matplotlib：

   ```text
   related_tests=['lib/matplotlib/tests/test_backend_ps.py::test_empty_line']
   vr_logs=verify_config: 未收集到任何测试 (target=lib/matplotlib/tests/...)
   ```

   已是 pytest 风格仍 0 收集 → 还需区分「格式错误」vs「路径不存在/导入失败」（可记同一 class 下的子症状）。

---

### E19 — Patch `original_lines`/`patched_lines` 为 list-repr / 非纯文本

| 字段 | 内容 |
|------|------|
| class_id | `E19_patch_lines_list_repr` |
| symptom | 导出 unified 出现 `-['…']` / `+['…', '…']`；`original_lines` 字面量含方括号与引号；补丁无法语义对齐源码 |
| affected_count | 1（matplotlib）；机制可迁移到任意 JSON list 字段泄漏 |
| fix_level | `parse_patches` / `CandidatePatch.from_dict`：list→`"\n".join`；拒绝/重试非字符串行字段 |
| generic_rule | 结构化补丁字段必须是源码文本，不是 Python list 的 `repr` |
| anti_overfit_check | FakeClient 返回 `original_lines: ["a","b"]` → 规范化为 `"a\nb"` 后可 apply |

**证据**

```text
# predictions.jsonl (matplotlib)
--- a/lib/matplotlib/backends/backend_ps.py
+++ b/lib/matplotlib/backends/backend_ps.py
@@ -1 +1 @@
-['                    stream.append(curr_stream)']
+['                    if curr_stream is not None:', '                        stream.append(curr_stream)']

# repair_state candidate_patches[0]
original_lines = "['                    stream.append(curr_stream)']"
patched_lines  = "['                    if curr_stream is not None:', ...]"
diff = ""
```

对比 django 同轮导出为正常 unified（`$`→`\Z`），说明闸门放行「看起来像 unified」的坏内容——E13 只保证有头，不保证行字段语义正确。

---

### E6a‴ — Apply / sibling 仍阻断或降质

| 字段 | 内容 |
|------|------|
| class_id | `E6a_apply_or_sibling` |
| symptom | `hunk_mismatch` / sibling 告警；或 apply 失败 → cand=0 / 空导出 |
| affected_count | 3（django sibling；matplotlib sibling；sympy apply_failed） |
| fix_level | apply 反馈已进 `_build_feedback`（R4→R5）；下一步：多站点 sibling 自动扩展、hunk 对齐重试预算 |
| generic_rule | 单点 replace 留下同 `original_lines` → 必须可行动反馈，而非仅 tag |
| anti_overfit_check | 合成文件两处相同片段，只改一处 → sibling 警告 + 下一轮 prompt 含原因 |

**证据**

```text
# sympy
tags=['apply_failed']
errs.patcher_apply='hunk_mismatch:sympy/core/basic.py'
feedback=补丁 JSON 解析成功但未能写入文件。原因: hunk_mismatch:...

# django / matplotlib patch_gen.log
[patcher] ⚠ ... original_lines still present after one-site apply
[patcher] ⚠ 无法应用补丁: django/...  (首轮)；次轮仍 sibling
```

---

### E14″ — Patch 阶段预算超时 → 空候选

| 字段 | 内容 |
|------|------|
| class_id | `E14_patch_phase_timeout` |
| symptom | `phase timeout (patch, 90s budget, consumed ≫90s)`；`status=timeout`；cand=0；导出空；`failure_detail=empty_model_patch` |
| affected_count | 2（astropy consumed 242s；pylint 350s） |
| fix_level | 大仓 patch 阶段预算 / 超时后若已有可 apply 候选则导出（E14 已有）；超时前强制 schema-only 收束 |
| generic_rule | 90s 墙钟预算与 LLM 长推理不匹配；超时不得默默丢已解析补丁 |
| status | 归因路径 OK；**预算策略仍 open** |

**证据**

```text
阶段超时: phase timeout (patch, 90s budget, consumed 242.2s)  # astropy
阶段超时: phase timeout (patch, 90s budget, consumed 349.7s)  # pylint
tags=['timeout']; cand=0; vr=None
```

---

### E18′ — Baseline schema retry（部分生效）

| 字段 | 内容 |
|------|------|
| class_id | `E18_parse_fail_degraded_empty` |
| status | **partial**：django/matplotlib `baseline_parse_recovered=True` 且最终 `cand=1`；matplotlib baseline 另有 `'in <string>' requires string as left operand, not list`（与 E19 同源） |
| note | R4 四题 `baseline=no patches in agent output`；R5 两题 degrade 后仍有候选 → E18 机制有效，但 verify/apply 仍挡 `fixed` |

---

### E11‴ — Retriever 仍全员 degrade

| 字段 | 内容 |
|------|------|
| class_id | `E11_retriever_degrade` |
| symptom | 五题 `retrieval_path=llm→degrade`（`agent_incomplete` / `tool_call_not_final`） |
| affected_count | 5 |
| note | FAIL_TO_PASS 已能灌进 related_tests（E17 路径），但 LLM retrieve 仍不稳；degrade 质量依赖规则检索 + issue 提示 |

---

### E20 — 非空 patch 但未 `fixed` / 未 verified（归因类）

| 字段 | 内容 |
|------|------|
| class_id | `E20_patch_without_fixed_status` |
| symptom | `model_patch` nonempty、`verified=false`、`failure_detail=patch_without_fixed_status`、`failure_class=agent` |
| affected_count | 2（django、matplotlib） |
| fix_level | 多为 E17′/E6a/E19 的下游表象；归因已区分于 `empty_model_patch`；勿为刷分绕过 verify |
| generic_rule | 「有 diff」≠「修复成功」；官方 harness 仍要求 FixLoop verify 通过（默认） |

---

## 3. 管道类（R3/R4）在本轮的表现

| 类 | R5 观察 |
|----|---------|
| E12 脏导出 | **仍生效**：无数百 KB 无关整仓 diff |
| E13 unified 头 | django 正常；matplotlib 有头但 **行内容为 list-repr**（需 E19） |
| E14 timeout 归因 | timeout+空候选 → `empty_model_patch`/`agent`（正确）；无「超时却假 none」 |
| E15 pending_verify | 无 `fixed+skip_verify` 样本 |
| E16 pip | **关闭** |

---

## 4. 建议下一轮（R6）优先级

| 顺序 | class_id | 理由 |
|------|----------|------|
| 1 | **E17′** FAIL_TO_PASS→pytest nodeid | 有补丁也 verify 不了；挡 `verified=true` |
| 2 | **E19** lines list-repr 规范化 | 否则 nonempty 导出是假补丁 |
| 3 | **E6a** apply/sibling | sympy 空导出 + django/mpl 降质 |
| 4 | **E14** patch 预算 / 超时收束 | astropy/pylint 空转超时 |
| 5 | E11 retrieve submit 率 | 降低对 degrade 的依赖 |
| 6 | 官方 harness | 仅对 `verified=true` 的 nonempty |

---

## 5. 产物索引

| 路径 | 用途 |
|------|------|
| `artifacts/swebench_lite_dev_live_r5/adapter_report.json` | 五题归因 / verified / patch |
| `artifacts/swebench_lite_dev_live_r5/predictions.jsonl` | django+matplotlib 非空；其余空 |
| `artifacts/swebench_lite_dev_live_r5/manifest.json` | `skip_verify=false` |
| `artifacts/swebench_lite_dev_live_r5/patch_gen.log` | pip/pytest 时序、timeout、sibling |
| `artifacts/swebench_repos/<id>/.agent/repairs/<run_id>/repair_state.json` | tags / cand / vr / related_tests |

---

## 7. R5→R6 代码修复状态（目标：产出 patch + FixLoop verified）

| class_id | 状态 | 机制 |
|----------|------|------|
| E19 | fixed | `normalize_patch_text_field` + `CandidatePatch.from_dict` |
| E17′ | fixed | unittest→pytest；basename 重定位；`_pick_test_path` 优先仓内文件 |
| E6a | strengthened | 相同 pre-image **全量** replace；多行 strip 键块匹配 |
| E14 | strengthened | `DEFAULT_PATCH_TIMEOUT_S=300`；超时保留已有 cand 不回滚 |
| E16 | closed (R5) | 仍保持 |
| E11 | open | retrieve 未动 |

回归：`tests/test_swebench_r6_verify_path.py`。下一动作：同口径 R6 真跑看 `verified` → 见 [`docs/SWE_BENCH_LITE_DEV5_R6_FAILURES.md`](SWE_BENCH_LITE_DEV5_R6_FAILURES.md)。
