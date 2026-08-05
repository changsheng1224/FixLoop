# SWE-bench Lite 开发集 R6 失败问题与证据

> 约束：`docs/SWE_BENCH_LITE_FIX_CONSTRAINTS.md`（改问题类，不特判题号）。  
> 前序：R1/R2 `docs/SWE_BENCH_LITE_DEV5_FAILURES.md`；R3–R5 见同目录 `*_R{3,4,5}_FAILURES.md`。  
> 本轮产物：`artifacts/swebench_lite_dev_live_r6/`  
> Agent：`artifacts/swebench_repos/<id>/.agent/runs|repairs/<run_id>/`  
> 记录日期：2026-08-05  
> 本轮代码：E19 lines 规范化、E17′ FAIL_TO_PASS→pytest、E6a 全量替换、E14 patch 预算 300s

---

## 0. 跑法与边界

```text
HF_HUB_OFFLINE=1
HTTPS_PROXY/HTTP_PROXY=http://127.0.0.1:7897
python -m src.benchmark.swebench
  --provider anthropic_compat --model-name fixloop-deepseek
  --output-dir artifacts/swebench_lite_dev_live_r6
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
| 相对 R5 | 非空 **2→3**（+sympy）；**仍 0 verified**；django related_tests 已成 pytest 路径，但收集仍为 0 |

日志：`artifacts/swebench_lite_dev_live_r6/patch_gen.log`

---

## 1. 五题总览

| instance_id | repair_status | patch_B | verified | failure_detail | 主要 tags / errors | duration_ms | run_id |
|-------------|---------------|---------|----------|----------------|-------------------|-------------|--------|
| astropy__astropy-12907 | exhausted | 0 | false | `empty_model_patch` | `parse_fail`；cand=0；vr=None | 170683 | `b586997a-…` |
| django__django-11099 | exhausted | **300** | false | `patch_without_fixed_status` | `verify_config`+`degraded_baseline`；cand=2；**pytest 路径已对仍 0 收集** | 313940 | `53ec9f33-…` |
| matplotlib__matplotlib-23964 | exhausted | **206** | false | `patch_without_fixed_status` | `verify_config`；sibling；行字段已非 list-repr | 204039 | `d1497625-…` |
| pylint-dev__pylint-6506 | exhausted | 0 | false | `empty_model_patch` | `parse_fail`+`degraded_baseline`；baseline 无补丁；vr 空收集 | 165566 | `d65d6f99-…` |
| sympy__sympy-20590 | exhausted | **201** | false | `patch_without_fixed_status` | `verify_config`；sibling；related=`test_immutable` 裸名 | 309457 | `10c94cf6-…` |

共性：`retrieval_path=llm→degrade`（五题）。

### 相对 R5 对照

| 项 | R5 | R6 | 解读 |
|----|----|----|------|
| nonempty | django、mpl | + **sympy** | 产出/导出改善 |
| E19 list-repr | mpl 导出 `-['…']` | mpl 为正常源码行 | **E19 生效** |
| E17′ 路径格式 | django=`test_x (mod.Class)` | django=`tests/auth_tests/...::Class::test_x` | **格式转换生效** |
| verify 收集 | 空收集 | 路径对仍 `total_tests=0` | 瓶颈变为 **E17″ 环境/收集** |
| E14 timeout 空 | astropy/pylint timeout | 无 phase timeout 文案；仍 parse 空 | **预算缓解超时标签**；parse 仍弱 |
| verified | 0 | 0 | 目标链路未打通 |

---

## 2. 问题类卡片（R6）

### E17″ — pytest target 文件存在仍「未收集到任何测试」

| 字段 | 内容 |
|------|------|
| class_id | `E17_pytest_collect_zero_despite_path` |
| symptom | `related_tests` / `_pick_test_path` 已是 `path::Class::test`；仓内文件存在；sandbox `pytest≈600–700ms`；`total_tests=0`；tag=`verify_config` |
| affected_count | 3+（django、matplotlib、pylint；sympy 为裸名变体） |
| fix_level | 区分「路径错」vs「收集期 import/配置失败」；django 等需正确 `DJANGO_SETTINGS_MODULE` / `pytest-django`；收集失败 stdout 写入 failure_logs；必要时用官方 test_patch 后再验 |
| generic_rule | 有合法 nodeid ≠ 能收集；大仓 verify 必须处理框架启动 |
| anti_overfit_check | 合成 django-like 需 settings 的 fixture：无 settings → 明确 env 错误，不标成补丁失败 |

**证据**

```text
# django related_tests（E17′ 已转换成功）
tests/auth_tests/test_validators.py::UsernameValidatorsTests::test_ascii_validator

# vr
all_passed=False total_tests=0
verify_config: 未收集到任何测试
  (target=tests/auth_tests/test_validators.py::UsernameValidatorsTests::test_ascii_validator)

# patch_gen.log
pip≈9s pytest≈650–736ms   # 进入了 pytest，但未收集到用例
```

matplotlib：`lib/matplotlib/tests/test_backend_ps.py::test_empty_line` 同样 0 收集。

---

### E20′ — 非空 patch 未 verified（表象类，R5 已记）

| 字段 | 内容 |
|------|------|
| class_id | `E20_patch_without_fixed_status` |
| symptom | nonempty + `verified=false` + `patch_without_fixed_status` |
| affected_count | 3（django、matplotlib、sympy） |
| note | 本轮主因是 **E17″ 空收集**，不是断言失败；勿为刷分跳过 verify |

---

### E6a⁗ — Sibling / apply 仍部分失败

| 字段 | 内容 |
|------|------|
| class_id | `E6a_apply_or_sibling` |
| symptom | `incomplete_sibling_pattern` 或 `无法应用补丁` / `hunk_mismatch` |
| affected_count | django（apply 失败但仍有导出 cand）、matplotlib/sympy（sibling 告警） |
| fix_level | 全量 replace 对「完全相同单行」有效；**多行块 / 上下文不完全相同** 的 sibling 仍需更强匹配或二次 apply；导出应优先反映「已写入磁盘」的 diff |
| evidence | django log：`无法应用补丁: validators.py` 两轮；仍导出 300B（候选态）。mpl/sympy sibling 文案仍在 |

---

### E18″ / parse — 空候选（astropy、pylint）

| 字段 | 内容 |
|------|------|
| class_id | `E18_parse_or_baseline_empty` |
| symptom | `parse_fail`；或 degrade 后 `baseline=no patches`；最终 cand=0 |
| affected_count | 2（astropy；pylint） |
| note | R6 未再刷 phase timeout（E14 预算）；但模型 JSON / baseline 仍空。pylint feedback 曾出现补丁预览，最终未落 cand |

---

### E17‴ — 裸 related_tests 未解析（sympy）

| 字段 | 内容 |
|------|------|
| class_id | `E17_bare_test_name` |
| symptom | `related_tests=['test_immutable']` → target=`test_immutable` → 0 收集 |
| fix_level | bare name → `find_test` / rglob `def test_immutable`；失败则 verify_config 并跳过假重试 |
| anti_overfit_check | 合成仓唯一 `test_immutable` 定义 → 自动解析到文件 |

---

### E11‴ — Retriever 仍 degrade

| 字段 | 内容 |
|------|------|
| class_id | `E11_retriever_degrade` |
| affected_count | 5 |
| note | 未在本轮改 submit 率；依赖 FAIL_TO_PASS + 规则降级 |

---

### 已验证关闭 / 改善（相对 R5 代码）

| 类 | R6 观察 |
|----|---------|
| E16 pip/`mkdir --user` | **仍关闭**；pip≈9s |
| E19 list-repr | **关闭**：mpl 导出为正常行插入，非 `-['…']` |
| E17′ unittest→pytest 字符串 | **部分关闭**：django 路径格式已正确；收集层见 E17″ |
| E14 90s 误杀 | **改善**：本轮无 `phase timeout (patch, 90s…)` |

---

## 3. 管道类表现

| 类 | R6 |
|----|-----|
| E12 脏导出 | 仍生效 |
| E13 unified 头 | nonempty 均有 `---`/`+++`；django 导出重复两段同文件 hunk（导出去重待加强） |
| E15 pending_verify | 无 |

---

## 4. 建议下一轮（R7）优先级 — 目标仍是 FixLoop `verified=true`

| 顺序 | class_id | 理由 |
|------|----------|------|
| 1 | **E17″** 收集期环境/导入 + **缺 test_patch** | 路径已对仍 0 tests → 挡死 verified |
| 2 | **E17‴** bare test 名解析 | sympy 类 |
| 3 | **E6a** 多行 sibling / apply 与导出一致 | django apply 失败仍导出 |
| 4 | **E18** parse/baseline 空 | astropy/pylint |
| 5 | E11 retrieve | 降 degrade |
| 6 | 官方 harness | 仅 `verified=true` 后 |

---

## 7. R6→R7 代码修复（进行中）

| 项 | 状态 | 说明 |
|----|------|------|
| verify 前应用 `test_patch` | **done** | `VerifyTestPatchOverlay`：打上 → verify → 还原；`repair(verify_test_patch=…)`；不进 model 导出 |
| 0 收集附带 pytest stdout | **done** | `python_runner` 追加 `pytest_stdout_tail` |
| test_patch 内新增 test 作 target | **done** | `extract_targets_from_test_patch` + bare `def` 搜索 |
| Django runtests runner | **done (能力向)** | `verify_env`：探测 `tests/runtests.py+django/`；label 映射；runtests 命令+输出解析；`DJANGO_SETTINGS_MODULE`；镜像预装 sqlparse/pytz/asgiref |
| **E17″ 沙箱依赖** | **done (R7→R8)** | 镜像预装 numpy/mpmath…；pip 保留真实 exit；`PYTHONPATH=/code[/lib|/src]`；声明 deps 尽力补装；pip 失败软继续 |
| **E6a′ apply/hunk** | **done (R7→R8)** | 折叠空白匹配；`describe_hunk_mismatch` 把 near= 真实行写入 feedback |
| **Patcher 精确落盘** | **done (能力向)** | `precise_apply` 对齐 `patch_engine`；`DISK GROUNDING` 真源片段；apply 失败最多 2 轮 recovery |
| **Patcher 工具化编辑** | **done (能力向)** | 默认 `ask`+`read_file`/`patch_file`；磁盘 diff→CandidatePatch；无落盘降级 JSON；`FIXLOOP_PATCHER_EDIT_MODE=json` 可强制旧路径 |
| **探索闭环** | **done (能力向)** | LLM 检索失败→`force_tool_explore`；规则检索补 snippet/find_test；`explore_quality` 门禁；`FIXLOOP_FORCE_EXPLORE=0` 可关 |
| **验证循环** | **done (能力向)** | `verify_diagnose` 分桶 env/collect/logic；feedback 带分桶+失败用例+可执行指导；连续 env×2 early-stop；失败 nodeid 注入 related_tests |
| **长程与止损** | **done (能力向)** | `StopLossTracker`：相同补丁/相同验证/空补丁震荡/无进展/env 早停；有新补丁或失败面变化则继续；止损→exhausted + 可 baseline 降级；tag=`no_progress` |
| **失败面利用** | **done (能力向)** | `fail_surface`：抽取断言+失败测试原文；注入 patcher/feedback；失败 nodeid 置顶 related_tests；`_pick_test_path` 优先再跑同一用例 |
| **检索/定位质量** | **done (能力向)** | `localize_quality`：traceback 确定性接地；存在性过滤；实现帧优先于测试帧；合并排序截断；规则检索关键词去噪（停用引号乱抽） |
| **可恢复长程** | **done (能力向)** | `long_horizon`：止损前可策略切换（扩搜/换假设→再收敛，默认 2 次）；回合中 checkpoint；resume 恢复 timings/策略并清软止损 |
| **定位天花板** | **done (能力向)** | `localize_expand`：测试→导入/调用→定义；issue 符号→定义；嫌疑函数调用方扩展；并入 `refine_suspects` 排序 |
| **可跑环境** | **done (能力向)** | `verify_env` 探测 django_runtests vs pytest；settings/PYTHONPATH；禁止空 label 全量；runtests 输出解析；Dockerfile +sqlparse/pytz/asgiref |

回归：…、`tests/test_localize_expand.py`、`tests/test_verify_env.py`。

> 镜像变更后需重建：`docker build -f sandbox/Dockerfile.python -t repair-agent/python-repair .`

---

## 5. 产物索引

| 路径 | 用途 |
|------|------|
| `artifacts/swebench_lite_dev_live_r6/adapter_report.json` | 五题归因 |
| `artifacts/swebench_lite_dev_live_r6/predictions.jsonl` | 3 非空 / 2 空 |
| `artifacts/swebench_lite_dev_live_r6/manifest.json` | `skip_verify=false` |
| `artifacts/swebench_lite_dev_live_r6/patch_gen.log` | pip/pytest/sibling |
| `artifacts/swebench_repos/<id>/.agent/repairs/<run_id>/repair_state.json` | tags / tests / vr |

---

## 6. 纪律提醒

> Lite 只提供失败形态；实现只修可迁移机制。拿掉 instance_id 后规则仍须成立。  
> FixLoop verify ≠ 官方 harness；本阶段以 `verified=true` 为成功口径。
