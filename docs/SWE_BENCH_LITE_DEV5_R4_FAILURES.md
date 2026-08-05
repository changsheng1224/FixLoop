# SWE-bench Lite 开发集 R4 失败问题与证据

> 约束：`docs/SWE_BENCH_LITE_FIX_CONSTRAINTS.md`（改问题类，不特判题号）。  
> 前序：R1/R2 `docs/SWE_BENCH_LITE_DEV5_FAILURES.md`；R3 `docs/SWE_BENCH_LITE_DEV5_R3_FAILURES.md`。  
> **下一轮 R5**：`docs/SWE_BENCH_LITE_DEV5_R5_FAILURES.md`。  
> 本轮产物：`artifacts/swebench_lite_dev_live_r4/`  
> Agent：`artifacts/swebench_repos/<id>/.agent/runs|repairs/<run_id>/`  
> 记录日期：2026-08-05

---

## 0. 跑法与边界

```text
HF_HUB_OFFLINE=1
python -m src.benchmark.swebench
  --provider anthropic_compat --model-name fixloop-deepseek
  --output-dir artifacts/swebench_lite_dev_live_r4
  --work-root artifacts/swebench_repos
  --repair-timeout-s 900
  # 未传 --skip-verify → FixLoop verifier 开启
  # 未传 --skip-clone → checkout --force + clean base_commit
```

| 项 | 值 |
|----|-----|
| Manifest | `skip_verify: false`，`run_harness: false`，`allow_unverified_harness: false` |
| 进程 | exit 1；报告 `ok: true`；`failure_summary`: `none=1 / agent=4` |
| 代码时点 | 含 R3 管道闸门（E12–E15 导出/归因）+ E11′ 等；verify 沙箱实际执行 |
| 相对 R3 | 非空 **5→1**；假 nonempty（脏仓/无头残片）被闸门挡住；**能力侧**四题 `cand=0` |

日志：`artifacts/swebench_lite_dev_live_r4/patch_gen.log`

---

## 1. 五题总览

| instance_id | repair_status | patch_B | verified | failure_detail | 主要 tags / errors | duration_ms |
|-------------|---------------|---------|----------|----------------|-------------------|-------------|
| astropy__astropy-12907 | exhausted | 0 | false | `empty_model_patch` | `parse_fail`, `degraded_baseline`；vr「未收集到任何测试」 | 133476 |
| django__django-11099 | exhausted | 0 | false | `empty_model_patch` | 同上 + sibling 告警；sandbox **pip/`mkdir --user`** | 239189 |
| matplotlib__matplotlib-23964 | exhausted | 0 | false | `empty_model_patch` | 同上；sandbox **pip/`mkdir --user`** | 115184 |
| pylint-dev__pylint-6506 | exhausted | 0 | false | `empty_model_patch` | `parse_fail`, `degraded_baseline`；vr「未收集到任何测试」 | 145663 |
| sympy__sympy-20590 | timeout | **267** | false | `timeout_with_patch` | `timeout`；`cand=2`；vr=None | 318557 |

共性：`retrieval_path=llm→degrade`（五题）。

### 与 R3「为何 R3 有导出、R4 空」

| 现象 | 解释 |
|------|------|
| R3 五题 nonempty | 含 **脏仓整 diff**（pylint/sympy）与 **无 unified 头残片**（django/mpl/astropy）；且 `--skip-verify` |
| R4 四题 empty | 本 run `candidate_patches=0`（parse/apply/baseline 失败）；E12 **禁止**无候选时整仓 fallback → 合法空串 |
| R4 sympy 267B | `cand=2` → 闸门放行 unified；E14 标 `timeout_with_patch`（`none`），`verified=false` |

结论：R4 空导出主要是 **能力/apply 失败被导出闸门如实暴露**，不是闸门误删已成功 apply 的补丁。

---

## 2. 问题类卡片（R4）

### E16 — Verifier 沙箱 pip / `mkdir --user` 失败

| 字段 | 内容 |
|------|------|
| class_id | `E16_sandbox_pip_mkdir_user` |
| symptom | verify 开启后 sandbox 报 `pip install failed`；日志含 `mkdir: unrecognized option '--user'` |
| affected_count | 2（django、matplotlib） |
| fix_level | sandbox / Docker 镜像入口脚本：勿对 `mkdir` 传 GNU 不支持的 `--user`；或换 pip 用户安装方式 |
| generic_rule | 任意仓 verify 都可能撞；与 instance 无关 |
| anti_overfit_check | 合成最小 Dockerfile + pip 步骤复现，不绑 django |
| status | **fixed (R4→R5)**：根因是 `exec_run(str)` 被 docker-py shlex 拆分，BusyBox `mkdir` 吃到 `--user`；现统一 `/bin/sh -c` |

**证据**

1. django / matplotlib `repair_state.verification_result`：

   ```text
   failure_logs: sandbox pip install failed: exit_code=1
   mkdir: unrecognized option '--user'
   build_log: pip install: exit_code=1 ... mkdir: unrecognized option '--user'
   ```

2. `patch_gen.log`：

   ```text
   [verifier] sandbox: create=270ms tar=8621ms pip=111ms pytest=0ms   # django
   [verifier] sandbox: create=284ms tar=13949ms pip=115ms pytest=0ms  # matplotlib
   ```

   `pytest=0ms` 与 pip 失败一致（未进入有效测试）。

---

### E17 — Verifier「未收集到任何测试」

| 字段 | 内容 |
|------|------|
| class_id | `E17_verify_no_tests_collected` |
| symptom | `verification_result.failure_logs` 含「未收集到任何测试」；`total_tests=0`；`all_passed=false` |
| affected_count | 2（astropy、pylint） |
| fix_level | test path 选择 / SWE FAIL_TO_PASS 映射进 pytest 参数；空收集不得伪装成「补丁错误」 |
| generic_rule | SWE 题应用实例测试列表或明确 skip，而非空跑 |
| anti_overfit_check | 合成 repo 无匹配 test path → 明确 `env`/`verify_config` tag |
| status | **fixed (R4→R5)**：issue 内 FAIL_TO_PASS → related_tests / `_pick_test_path`；空收集日志/tag=`verify_config` |

**证据**

- astropy / pylint `verification_result`：`failure_logs=['未收集到任何测试']`，`passed=failed=error=0`。  
- 日志：`pytest=713ms` / `714ms` 仍可能对应「收集阶段结束但 0 tests」。

---

### E18 — Patcher parse_fail → degraded_baseline → cand=0

| 字段 | 内容 |
|------|------|
| class_id | `E18_parse_fail_degraded_empty` |
| symptom | `failure_tags` 含 `parse_fail` + `degraded_baseline`；`agent_errors.baseline=no patches in agent output`；`candidate_patches=[]` → 导出空 |
| affected_count | 4（astropy、django、matplotlib、pylint） |
| fix_level | patcher 结构化输出 / schema 重试；baseline 降级路径也须能产出最小 diff；与 E6a 同类加强 |
| generic_rule | 降级不得只记 tag 而不尝试可 apply 的候选 |
| anti_overfit_check | FakeClient 返回非法 JSON → 有限 retry 后仍空则 tag，但不得依赖脏仓导出 |
| status | **fixed (R4→R5)**：baseline `ask` 无补丁且未改文件 → `complete_once` schema 微重试 |

**证据**

四题 `repair_state`：`status=exhausted`，`cand=0`，`tags` 含 `parse_fail`/`degraded_baseline`，`errs.baseline='no patches in agent output'`。  
Adapter：`failure_detail=empty_model_patch`。

---

### E6a‴ — Apply / sibling 失败仍 exhausted 空导出

| 字段 | 内容 |
|------|------|
| class_id | `E6a_apply_or_sibling_empty` |
| symptom | 日志「无法应用补丁」或 sibling 残留告警；最终仍 `cand=0` / 空导出 |
| affected_count | 1+（django 明确） |
| fix_level | apply 反馈 retry（E6a′ 已有路径）在 verify 开、干净树上仍要生效；sibling 告警升级为可行动 feedback |
| generic_rule | hunk 与预读不一致 → 结构化原因进下一轮 patcher |

**证据**

```text
# patch_gen.log
[patcher] ⚠ 无法应用补丁: django/contrib/auth/validators.py

# repair_state agent_errors
incomplete_sibling_pattern: django/contrib/auth/validators.py:
  original_lines still present after one-site apply (...)
baseline: no patches in agent output
```

---

### E14′ — Patch 阶段超时但仍有候选（sympy）

| 字段 | 内容 |
|------|------|
| class_id | `E14_patch_phase_timeout`（R3 已记；R4 复现） |
| symptom | `phase timeout (patch, 90s…)`；`cand≥1`；导出 unified；`verified=false`；detail=`timeout_with_patch` |
| affected_count | 1（sympy） |
| 状态 | **归因已修**（E14→`none`/`timeout_with_patch`）；预算/超时后是否续跑 verify 仍待办 |
| 证据 | 日志 `consumed 123.4s`；patch 含 `sympy/core/basic.py` + `symbol.py` 的 `__slots__` 向改动；`vr=None` |

---

### E11‴ — Retriever 仍全员 degrade

| 字段 | 内容 |
|------|------|
| class_id | `E11_retriever_degrade` |
| symptom | 五题 `retrieval_path=llm→degrade` |
| affected_count | 5 |
| 说明 | R4 日志未再刷「输出不是 JSON 对象」（E11′ 软校验可能已生效）；但仍未稳定 `submit_retrieved_context` |
| fix_level | 提高 submit 率 / 步数策略；degrade 质量单独评 |

---

## 3. 管道类（R3 已修）在本轮的表现

| 类 | R4 观察 |
|----|---------|
| E12 脏导出 | **生效**：无再出现数百 KB 无关文件 patch；四题诚实 empty |
| E13 unified 头 | **生效**：唯一 nonempty（sympy）带 `---`/`+++` |
| E14 timeout 归因 | **生效**：sympy → `timeout_with_patch` / `none`，非笼统 empty agent |
| E15 pending_verify | 本轮无 `fixed+skip_verify` 样本（verify 开启） |

---

## 4. 建议下一轮优先级

| 顺序 | class_id | 理由 | R4→R5 |
|------|----------|------|-------|
| 1 | **E16** sandbox pip/`mkdir --user` | 否则开 verify 也无法判定补丁对错 | **已修根因** |
| 2 | **E18** parse_fail + degraded 空候选 | 否则 nonempty 上不去 | **已修** |
| 3 | **E17** 无测试收集 | SWE 路径映射，避免假失败 | **已修** |
| 4 | E6a apply/sibling 反馈 | django 类复现 | **加强** |
| 5 | E14 超时后 verify / 预算 | sympy 已有 patch 未 verified | 待办 |
| 6 | 官方 harness | 仅对 `verified=true` 的 nonempty | 待办 |

---

## 5. 产物索引

| 路径 | 用途 |
|------|------|
| `artifacts/swebench_lite_dev_live_r4/adapter_report.json` | 五题归因 / verified / patch |
| `artifacts/swebench_lite_dev_live_r4/predictions.jsonl` | 仅 sympy 非空（267B） |
| `artifacts/swebench_lite_dev_live_r4/manifest.json` | `skip_verify=false` |
| `artifacts/swebench_lite_dev_live_r4/patch_gen.log` | sandbox / apply / timeout |
| `artifacts/swebench_repos/<id>/.agent/repairs/<run_id>/repair_state.json` | tags / cand / vr |

---

## 6. 纪律提醒

> Lite 只提供失败形态；实现只修可迁移机制。拿掉 instance_id 后规则仍须成立。

---

## 7. R4→R5 修复状态（2026-08-05）

| class_id | 状态 | 机制 |
|----------|------|------|
| E16 | fixed | `SandboxManager.execute` → `["/bin/sh","-c",cmd]`（防 docker-py shlex 把 `--user` 交给 mkdir）；entrypoint 尊重 HOME/PYTHONUSERBASE；pip 用 `python -m pip` |
| E17 | fixed / **E17′ open→fixed in code** | issue 内 FAIL_TO_PASS → pytest target（unittest 风格转换 + 文件重定位）；空收集 → `verify_config` |
| E18 | fixed | degrade baseline 无补丁时 schema `complete_once` 微重试 |
| E19 | **fixed in code (R5→R6)** | `normalize_patch_text_field`：list / list-repr → 源码文本 |
| E6a | **partial→strengthened** | 相同 `original_lines` 全量替换；多行 strip 键匹配 |
| E14 | **partial→strengthened** | patch 默认预算 90→300s；超时若已有 cand 不回滚落盘 |
| E11‴ | open | 未改 retriever submit 率 |

回归：`tests/test_swebench_r4_root_fixes.py`、`tests/test_sandbox_manager.py`（execute shell wrap）。
