# SWE-bench Lite 开发集（5 题）失败问题与证据

> 基于 `docs/SWE_BENCH_LITE_FIX_CONSTRAINTS.md`：下列均为**问题类**；instance 仅作证据样本，实现时不得特判题号/repo。  
> 产物根：`artifacts/swebench_lite_dev_live/`  
> Agent run：`artifacts/swebench_repos/<instance_id>/.agent/runs|repairs/<run_id>/`  
> 记录日期：2026-08-05（首轮 live + WSL harness django-smoke）  
> **R3（2026-08-05 五题补丁生成）**：[`docs/SWE_BENCH_LITE_DEV5_R3_FAILURES.md`](SWE_BENCH_LITE_DEV5_R3_FAILURES.md)。  
> **R4（verify 开启）**：[`docs/SWE_BENCH_LITE_DEV5_R4_FAILURES.md`](SWE_BENCH_LITE_DEV5_R4_FAILURES.md)。  
> **R5（E16/E17/E18 修复后 verify 复跑）**：[`docs/SWE_BENCH_LITE_DEV5_R5_FAILURES.md`](SWE_BENCH_LITE_DEV5_R5_FAILURES.md)。  
> **R6（E19/E17′/E6a/E14 后 verify）**：[`docs/SWE_BENCH_LITE_DEV5_R6_FAILURES.md`](SWE_BENCH_LITE_DEV5_R6_FAILURES.md)。

---

## 1. 五题总览

| instance_id | repair_status | nonempty patch | 主要命中类 | run_id |
|-------------|---------------|----------------|------------|--------|
| django__django-11099 | fixed | 是 | E1、E7 | `11a4e3e3-0b24-467a-ba34-acaa6e8b4fc0` |
| matplotlib__matplotlib-23964 | failed | 否 | E2、E3、E5、E6a | `67e5ecec-72fc-4001-b8ce-c0504855456d` |
| pylint-dev__pylint-6506 | failed + parse_fail | 否 | E2、E5、E6a | `601e92b5-8e5e-4b33-a8fe-0de3fe93c4ff` |
| sympy__sympy-20590 | failed + parse_fail | 否 | E3、E5、E6a | `c9467982-df9a-482a-a514-940317e53933` |
| astropy__astropy-12907 | failed | 否 | E6a；首轮另有 E6b | `13ad6fdf-656f-4b3c-b8df-563350c881b7` |

首轮 `adapter_report.json`：`failure_summary` ≈ agent 4 / env 1；`predictions.jsonl` 仅 django 非空。  
WSL harness（django-smoke）：`harness.ok=true`，django 因 E1 进入 `error_instances`（非 Resolved）。

---

## 2. 问题类卡片（含证据）

### E1 — Patch 含 CRLF，官方 harness 无法 apply

| 字段 | 内容 |
|------|------|
| class_id | `E1_crlf` |
| symptom | 非空 patch 在容器内 `git apply` / `patch` 失败 |
| affected_count | 1（django；唯一 nonempty） |
| fix_level | `adapter_export` |
| generic_rule | 一切 `model_patch` / reexport 统一 LF，去掉 CR |
| anti_overfit_check | 不依赖 django / validators.py；单测用合成 `\r\n` diff |

**证据**

1. `artifacts/swebench_lite_dev_live/predictions.jsonl`（django 行）：patch 内 `CR count=8`，`repr` 可见 `\r\n @deconstructible\r\n`。
2. `artifacts/swebench_lite_dev_live/logs/run_evaluation/fixloop-1785873312/fixloop-deepseek/django__django-11099/run_instance.log`：

   ```text
   Hunk #1 FAILED at 7 (different line endings).
   1 out of 1 hunk FAILED
   ```

3. `artifacts/swebench_lite_dev_live/fixloop-deepseek.fixloop-1785873312.json`：`error_instances=1`，`resolved_instances=0`。

**acceptance（类级）**：规范化后无 CR；django-smoke 不再因 line endings 失败（不保证 Resolved）。

---

### E7 — 补丁不完整（同类 pattern 只改一半）

| 字段 | 内容 |
|------|------|
| class_id | `E7_incomplete_sibling_pattern` |
| symptom | 同文件多处相同结构只修一处 |
| affected_count | 1（django） |
| fix_level | patcher 自检 / 轻量 verifier |
| generic_rule | 提交前同文件扫描与本次编辑相同的结构 pattern；**不写符号名** |
| anti_overfit_check | 禁止 `if "UnicodeUsername"`；夹具为同文件两处假锚点 |

**证据**

1. `.../django__django-11099/.agent/repairs/11a4e3e3-.../repair_state.json`：`candidate_patches` 仅一行 ASCII：`$` → `\Z`；`status=fixed`。
2. 工作树 `artifacts/swebench_repos/django__django-11099/django/contrib/auth/validators.py`：
   - `ASCIIUsernameValidator.regex` 已是 `\Z`
   - `UnicodeUsernameValidator.regex` 仍为 `$`
3. 同文件 blackboard/suspects 曾覆盖两处（含 Unicode 行附近），但只落地一处。

**acceptance**：合成双处相同锚点、只 patch 一处 → warning 或再检；不保证业务 Resolved。

---

### E2 — Issue / 栈绝对路径未清洗

| 字段 | 内容 |
|------|------|
| class_id | `E2_abs_path_hallucination` |
| symptom | `suspect_files` 指向外机/临时 venv 绝对路径，不在 workspace |
| affected_count | 2（matplotlib、pylint） |
| fix_level | issue 预处理 / localizer 输入 |
| generic_rule | 绝对路径 → workspace 相对；无法映射则丢弃并降置信 |
| anti_overfit_check | 不写死用户名或 `C:/temp/...`；夹具用任意绝对前缀 |

**证据**

1. matplotlib `trace.jsonl` → `prompt_routing.suspect_files` 含：

   ```text
   C:/temp/matplotlib_save_ps/save_ps.py
   C:/temp/matplotlib_save_ps/venv/lib/site-packages/matplotlib/...
   ```

   路径：`artifacts/swebench_repos/matplotlib__matplotlib-23964/.agent/runs/67e5ecec-.../trace.jsonl`

2. pylint 同事件含：

   ```text
   /Users/markbyrne/venv310/bin/pylint
   /Users/markbyrne/programming/pylint/pylint/...
   ```

   路径：`artifacts/swebench_repos/pylint-dev__pylint-6506/.agent/runs/601e92b5-.../trace.jsonl`

---

### E3 — Gateway 角色拒绝读码工具（与 Skill 建议错位）

| 字段 | 内容 |
|------|------|
| class_id | `E3_gateway_role_mismatch` |
| symptom | `role_not_allowed`；探索无效仍耗步数 |
| affected_count | 2（matplotlib 重、sympy 轻） |
| fix_level | `gateway` / skill↔role 绑定 |
| generic_rule | 角色-工具矩阵与 `suggested_tools` 一致；拒绝返回可行动错误 |
| anti_overfit_check | 不按 repo 开白名单；测假 role + 假 tool |

**证据**

1. matplotlib `report.json`：`tool_rejections_by_gate: {gateway: 4}`；`permission_denied_by_tool`: `inspect_file`/`find_test`/`read_file`/`grep` 各 1。
2. 同 run `trace.jsonl` → `tool_executed`：

   ```text
   tool_status=rejected
   tool_error_code=permission_denied
   rejection_layer=gateway
   rejection_reason=role_not_allowed
   agent=retriever
   ```

3. 同 run Skill：`python_type_error_fix`，`suggested_tools: [stack_parse, ast_parse, search, patch_file]`（与实际可调工具错位）。
4. sympy：`inspect_file` 同样 `role_not_allowed`（1 次）→ `.../c9467982-.../`。

---

### E5 — step_limit；无效步挤占预算

| 字段 | 内容 |
|------|------|
| class_id | `E5_step_budget` |
| symptom | `tool_steps > 4`；拒绝/空转占满；patcher/localizer 几乎无 tool |
| affected_count | 3+（matplotlib、pylint、sympy；django retriever 亦 step_limit） |
| fix_level | `budget` / loop limits |
| generic_rule | 分层预算；拒绝步降权；保证 patch 最低步数 |
| anti_overfit_check | 不为单题改 max_steps |

**证据**

1. matplotlib / pylint / sympy：`run_finished` → `stop_reason=step_limit`，`stop_reason_detail=tool_steps > 4`。
2. matplotlib：总 tool 步 6，其中 gateway 拒绝 4 次后仍失败。
3. 各失败题 `report.json`：`tool_usage_by_agent` 中 `localizer=0`、`patcher=0`（django 成功时 patch 亦曾为 0 tool，靠 candidate 落地——对比用）。

---

### E6a — parse_fail / 空 candidate / apply_failed → 空 patch

| 字段 | 内容 |
|------|------|
| class_id | `E6a_empty_or_parse_fail` |
| symptom | 无有效 `candidate_patches`，最终 `model_patch=""` |
| affected_count | 4（非 django） |
| fix_level | patcher parse / apply 反馈 |
| generic_rule | Schema 失败短 retry；区分 parse / apply / empty_generation |
| anti_overfit_check | 非法 JSON/坏 diff 夹具，不依赖真实 Lite 树 |

**证据**

1. pylint / sympy `repair_state.json`：`failure_tags: ["parse_fail"]`，`candidate_patches: []`。
2. astropy / matplotlib `repair_state.json`：`agent_errors.patcher_apply: "apply_failed"`，`candidate_patches: []`。
3. reexport 后各 `instances/<id>/result.json`：`failure_detail: empty_after_reexport`（django 除外）。
4. `predictions.jsonl`：四题 `"model_patch": ""`。

---

### E6b — 二进制/非 UTF-8 导出崩溃（管道误判）

| 字段 | 内容 |
|------|------|
| class_id | `E6b_binary_utf8_export` |
| symptom | 导出 diff 时 codec 异常，失败被标成 agent |
| affected_count | 4（首轮 adapter；django 幸免） |
| fix_level | `adapter_export` |
| generic_rule | 只导出文本；二进制跳过并记 trace |
| anti_overfit_check | 夹具放假 `.png`；进程不因 codec 崩 |
| 状态 | 已有 binary-safe reexport；需保持回归 |

**证据**

首轮 `artifacts/swebench_lite_dev_live/adapter_report.json`：

| instance | error 摘要 |
|----------|------------|
| astropy | `'utf-8' codec can't decode byte 0x89`（PNG） |
| pylint | 同上 `0x89` |
| sympy | 同上 `0x89` |
| matplotlib | `byte 0xff` |

---

### E6c — Windows 无法跑官方 harness（环境）

| 字段 | 内容 |
|------|------|
| class_id | `E6c_harness_unix` |
| symptom | 无 `resource` 模块，官方 harness 不可用 |
| affected_count | 首轮全部（含已有 patch 的 django） |
| fix_level | 文档 / WSL backend |
| generic_rule | Windows → `--harness-backend wsl` |
| 状态 | **已打通**（django-smoke `harness.ok=true`）；迭代降优先 |

**证据**

1. 首轮 `adapter_report.json` → `harness.error`：`official swebench.harness requires Unix (resource module)...`
2. 后：`harness_only_report.json` → `harness.ok=true`，`backend=wsl`（django 仍因 E1 未 Resolved）。

---

## 3. 对 Agent 有价值的修复优先级

按约束「先管道后能力」：

| 顺序 | class_id | 价值 | 实现状态（2026-08-05） |
|------|----------|------|------------------------|
| 1 | E1_crlf | 否则 Resolved 全是噪声 | **已修**：`normalize_patch_lf` → export / reexport / predictions / harness filter |
| 2 | E3_gateway_role_mismatch | 直接决定能否读码 | **已修**：取消 skill `restrict_to` 跨角色白名单；ACL 保持角色表 |
| 3 | E2_abs_path_hallucination | 定位输入质量 | **已修**：`relativize_suspect_path` + stack/Issue 适配 |
| 4 | E5_step_budget | 同成本更多有效尝试 | **已修**：gateway `rejected` 不计入 `tool_steps` |
| 5 | E6a_empty_or_parse_fail | 抬 nonempty patch | **已修**：patcher schema 微重试 + `parse_fail` 标记 |
| 6 | E7_incomplete_sibling_pattern | 减少半修好 | **已修**：同文件 `original_lines` 残留告警 → feedback |
| — | E6b | 保持回归 | 已有 binary-safe；保持 |
| — | E6c | 运维/文档 | WSL harness 已通 |

单测：`tests/test_swebench_flywheel_fixes.py`（合成夹具）。

---

## 4. 关键产物索引

| 文件 | 用途 |
|------|------|
| `artifacts/swebench_lite_dev_live/adapter_report.json` | 首轮 5 题汇总、UTF-8 导出错误、Windows harness 不可用 |
| `artifacts/swebench_lite_dev_live/predictions.jsonl` | 最终 patch（含 CRLF） |
| `artifacts/swebench_lite_dev_live/reexport_report.json` | binary-safe 重导出后 nonempty=1 |
| `artifacts/swebench_lite_dev_live/harness_only_report.json` | WSL django-smoke |
| `artifacts/swebench_lite_dev_live/fixloop-deepseek.fixloop-1785873312.json` | 官方 Resolved/error 计数 |
| `artifacts/swebench_lite_dev_live/logs/run_evaluation/.../run_instance.log` | line endings 原文 |
| `artifacts/swebench_repos/*/.agent/runs/<run_id>/report.json` | failure_tags、tool 拒绝、step_limit |
| `artifacts/swebench_repos/*/.agent/runs/<run_id>/trace.jsonl` | prompt_routing、tool_executed |
| `artifacts/swebench_repos/*/.agent/repairs/<run_id>/repair_state.json` | candidate_patches、agent_errors |

---

## 5. 纪律（摘自约束）

> Lite case 只提供失败形态的样本；代码只实现可迁移的机制。若修复离开这 5 个 id 就失效，则视为过拟合，不予合并。

改码前为拟修类补全 Failure Card（见约束 §5），一轮一类，合成夹具优先于真实 Lite 树断言。

---

## 6. 复跑 R2（非 django 四题，2026-08-05）

> 产物：`artifacts/swebench_lite_dev_live_r2/`  
> 命令：`--skip-clone`，无 harness；flywheel 修复（E1/E2/E3/E5/E6a/E7）已合入后的 live。  
> 日志：`artifacts/swebench_lite_dev_live_r2/patch_gen.log`

### 6.1 四题总览

| instance_id | repair_status | nonempty | 主要命中类 | run_id |
|-------------|---------------|----------|------------|--------|
| astropy__astropy-12907 | failed | 否 | E6a（parse_fail） | `c3513380-76ed-4308-b783-d474e04394b7` |
| matplotlib__matplotlib-23964 | failed | 否 | E2′、E3′、E5′、E8、E9、E10 | `5b201dfe-65d8-4fd6-b132-b39f2d14c0ce` |
| pylint-dev__pylint-6506 | fixed | 是（92B，LF） | （无空 patch；待 harness） | `ee93e71e-2ec8-4d4d-ae42-a9529eb22958` |
| sympy__sympy-20590 | failed | 否 | E6a′（apply_failed）、E8 | `5d4186a5-cce0-49c3-9379-8c3587a29a6b` |

`adapter_report.json`：`none=1 / agent=3`；相对首轮同四题 nonempty **0→1**。

控制台共性：四题均出现 `schema 校验: 输出不是 JSON 对象`（来自 `parse_retrieved_context`，见 `src/repair/output_parsers.py`）。

### 6.2 相对首轮：已缓解 / 仍残留

| 首轮类 | R2 观察 |
|--------|---------|
| E2 绝对盘符 | **部分缓解**：不再见 `C:/temp/...` 整串进 `suspect_files`；matplotlib 仍残留相对化后的 **仓外路径** `temp/matplotlib_save_ps/save_ps.py`（见 E2′） |
| E3 大面积 role 拒绝 | **大幅缓解**：read/inspect/find_test 成功；仍有 **stack_parse ×1** `role_not_allowed`（见 E3′） |
| E5 拒绝步占预算 | **部分缓解**：rejected 不再主导；matplotlib 仍 `step_limit`，且与 E10 goal_drift 耦合 |
| E6a parse/空补丁 | **仍重**：astropy `parse_fail`；mpl/sympy 解析后 apply 失败 → 空 export |
| E1 CRLF | R2 pylint 非空 patch **CR=0**（未跑 harness） |

### 6.3 新/细化问题类（含证据）

#### E2′ — 仓外相对路径仍进入 suspect / patch 目标

| 字段 | 内容 |
|------|------|
| class_id | `E2b_foreign_relative_path` |
| symptom | 绝对路径被剥前缀后仍指向非 workspace 文件；patcher 拒写 |
| affected_count | 1（matplotlib） |
| fix_level | path gate：相对化后须 `exists` 于 repo，否则丢弃 |
| generic_rule | 不保留 `temp/` 等仓外相对路径作嫌疑/补丁目标；issue 栈应映射到 repo 内帧 |
| anti_overfit_check | 任意 `tmpdir/repro.py` 夹具，不写死 matplotlib |

**证据**

1. `prompt_routing` / `repair_plan.suspect_files`：`["temp/matplotlib_save_ps/save_ps.py"]`  
   → `.../matplotlib.../runs/5b201dfe-.../trace.jsonl`、`repair_checkpoint.json`
2. Localizer 已给出正确仓内点：`lib/matplotlib/backends/backend_ps.py:673`
3. `patch_gen.log`：

   ```text
   [patcher] ⚠ 拒绝补丁（路径不在 repo 或文件不存在）: 'temp/matplotlib_save_ps/save_ps.py'
   [patcher] 补丁解析成功但未写入任何文件
   ```

4. `agent_errors`: `patcher_apply=apply_failed`

---

#### E3′ — Skill 仍建议 Retriever 禁用工具（残余）

| 字段 | 内容 |
|------|------|
| class_id | `E3b_skill_tool_not_in_role_acl` |
| symptom | Skill 提示 `stack_parse`，gateway 对 retriever 仍 `role_not_allowed` |
| affected_count | 1（matplotlib；gateway 拒绝 1） |
| fix_level | skill `suggested_tools` ∩ 角色 ACL；或提示层过滤 |
| generic_rule | 不得向角色提示其 ACL 外工具 |
| anti_overfit_check | 合成 skill 建议非法 tool → 提示被滤掉 |

**证据**

1. Retriever `task_state.json` Skill 提示：`工具序: stack_parse → ast_parse → search → patch_file`
2. `trace.jsonl` → `tool_executed`：

   ```text
   tool=stack_parse
   tool_status=rejected
   rejection_reason=role_not_allowed
   agent=retriever
   ```

3. 同 run：`inspect_file` / `read_file` / `find_test` 均为 `success`（对比首轮大面积拒绝）。
4. `report.json`：`permission_denied_by_tool.stack_parse=1`；`tool_rejections_by_gate.gateway=1`

---

#### E5′ / E10 — step_limit 与 goal_drift 误杀（以仓外 suspect 为「目标文件」）

| 字段 | 内容 |
|------|------|
| class_id | `E10_goal_drift_wrong_anchor` |
| symptom | StepGuard 把 issue 里的 repro 文件当目标，把正确的库内文件当「无关」 |
| affected_count | 1（matplotlib） |
| fix_level | `step_guard` 锚点：优先 blackboard/suspect **仓内**路径，忽略仓外 |
| generic_rule | drift 判定不得以不存在于 workspace 的路径为唯一锚点 |
| anti_overfit_check | 合成：目标 `temp/x.py` 不存在、工具读 `lib/ok.py` → 不得 goal_drift |

**证据**

1. `task_state.json`（retriever）：

   ```text
   stop_reason_detail: 连续 4 步操作与任务无关的文件（目标: save_ps.py，当前: backend_ps.py）
   ```

   同时 `stop_reason` 链路含 `goal_drift`；最终 `agent_report.retriever`：`stop_reason=step_limit`，`tool_steps=5`，`tool_steps > 4`。
2. Issue 原文栈与嫌疑均为 `C:\temp\matplotlib_save_ps\save_ps.py`；真正缺陷在 `backend_ps.py`（localizer 已定位）。
3. Trace：两次 `goal_drift` 事件后仍继续 tool，直至 step_limit。

---

#### E8 — similar_snippets Windows 盘符被拆成 `file="C"`

| 字段 | 内容 |
|------|------|
| class_id | `E8_win_path_colon_split` |
| symptom | 检索片段元数据把 `C:\...` 在 `:` 处拆开 |
| affected_count | 4（本轮全部） |
| fix_level | snippet serializer / path 解析 |
| generic_rule | Windows 盘符路径不得按首个 `:` 分割 |
| anti_overfit_check | 夹具路径 `C:\repo\a.py` → `file` 完整相对或完整绝对 |

**证据**

四题 `retrieved_context.similar_snippets[0]` 形态均为：

```json
{"file": "C", "line": "\\Users\\haoyu\\Documents\\FixLoop\\artifacts\\swebench_repos\\...\\....py", "text": "..."}
```

样本：astropy / matplotlib / pylint / sympy 各自 `repair_state.json` / `repair_checkpoint.json`。

---

#### E6a′ — parse 成功但 apply 失败 → 空 patch（与纯 parse_fail 区分）

| 字段 | 内容 |
|------|------|
| class_id | `E6a_apply_failed_empty_export` |
| symptom | 控制台「补丁解析成功但未写入」；`candidate_patches=[]`；export 空 |
| affected_count | 2（matplotlib 路径拒写；sympy hunk 应用失败） |
| fix_level | apply 反馈进 retry；失败原因结构化进 `failure_tags` |
| generic_rule | `apply_failed` 应带 path+reason；触发有限次 patcher 重试 |
| anti_overfit_check | 合成错 hunk / 错路径，断言 tag 与 retry，不依赖 Lite |

**证据**

1. sympy `patch_gen.log`：

   ```text
   [patcher] ⚠ 无法应用补丁: sympy/core/basic.py
   [patcher] 补丁解析成功但未写入任何文件
   ```

2. sympy `agent_errors`: `{"patcher_apply": "apply_failed"}`；suspect 为 `sympy/core/basic.py:15`（`__slots__` 叙事）。
3. matplotlib：见 E2′（路径拒写亦归 apply_failed）。
4. astropy：**纯 parse**：`failure_tags=["parse_fail"]`，`patcher_parse_failed=true`，`parse_apply_ms≈28515`（长时间解析仍失败）；`candidate_patches=[]`。

---

#### Schema 噪声 — Retriever 非对象输出

四题日志均有 `schema 校验: 输出不是 JSON 对象`。对应 `parse_retrieved_context`：模型常回 tool-call / 非 dict，编排降级规则检索（`retrieval_path=rule`）。**非空 patch 的阻断主因不是它**（pylint 同样有该日志仍 fixed），但会污染上下文质量；与 E6a schema 微重试（patcher）不同通道。

> **后续修复（E11）**：Retriever 改为终态工具 `submit_retrieved_context`（Tool Calling Structured Output）；自由文本 tool-call 归类为 `tool_call_not_final`，不再笼统报「不是 JSON 对象」。
>
> **E11′（2026-08-05）**：step_limit/`<final>` 归类为 `agent_incomplete`（INFO 软校验）；retriever 关闭 `json_mode`；`max_steps` 4→6 以便留出 submit。

### 6.4 R2 产物索引

| 文件 | 用途 |
|------|------|
| `artifacts/swebench_lite_dev_live_r2/adapter_report.json` | 四题汇总 |
| `artifacts/swebench_lite_dev_live_r2/predictions.jsonl` | pylint 非空；余空；CR=0 |
| `artifacts/swebench_lite_dev_live_r2/patch_gen.log` | 拒写 / 无法应用 / schema 原文 |
| `artifacts/swebench_repos/<id>/.agent/runs/<run_id>/` | report / trace / task_state |
| `artifacts/swebench_repos/<id>/.agent/repairs/<run_id>/repair_state.json` | agent_errors / candidates |

### 6.5 建议下一轮优先级（类级）

| 顺序 | class_id | 理由 | 状态（2026-08-05） |
|------|----------|------|---------------------|
| 1 | E2′ + E10 | 同一根因：仓外路径当锚点 → 错 patch 目标 + 误杀检索 | **已修**：`repo_root` 下不存在则丢弃；StepGuard 仅用仓内锚点 |
| 2 | E6a′ apply 反馈 | sympy/mpl 已「解析成功」却零落地 | **已修**：`last_apply_errors` + `FailureTag.APPLY_FAILED` + retry feedback |
| 3 | E3′ skill∩ACL | 残余 1 类拒绝，成本低 | **已修**：Skill 工具序 ∩ `REPAIR_PERMISSION_TABLE` |
| 4 | E8 路径序列化 | 四题污染 retrieved_context | **已修**：`_split_grep_path_line`（盘符安全） |
| 5 | E6a parse_fail | astropy；加强 patcher 结构化输出 | 待办 |
| — | pylint harness | 唯一 nonempty，先评 Resolved 再谈质量 | 待办 |

单测扩展：`tests/test_swebench_flywheel_fixes.py`（E2′/E8/E3′/E6a′/E10）。
