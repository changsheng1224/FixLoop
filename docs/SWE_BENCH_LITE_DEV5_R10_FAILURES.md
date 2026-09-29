# SWE-bench Lite 开发集 R10 失败问题与证据

> 约束：`docs/SWE_BENCH_LITE_FIX_CONSTRAINTS.md`（改问题类，不特判题号）。
> 前序：R3–R6 见同目录 `*_R{3,4,5,6}_FAILURES.md`。
> 本轮产物：`artifacts/swebench_lite_dev_live_r10/`
> Agent：`artifacts/swebench_repos/<id>/.agent/runs|repairs/<run_id>/`
> 记录日期：2026-08-06
> 本轮代码：Patcher Primary（Phase A–C）— 规则种子、`apply_patch`、Critic rules_first、ProgressEmitter；**本轮故意 `--skip-verify`**

旧目录备份（非本轮）：`artifacts/swebench_lite_dev_live_r10_legacy_20260805/`

---

## 0. 跑法与边界

```text
FIXLOOP_REPAIR_MODE=patcher_primary
FIXLOOP_CRITIC_MODE=rules_first
FIXLOOP_PROGRESS=1
FIXLOOP_PROGRESS_HEARTBEAT=1
FIXLOOP_PATCHER_COMPACT=1
FIXLOOP_PROGRESS_JSONL=artifacts/swebench_lite_dev_live_r10/progress.jsonl

python -m src.benchmark.swebench
  --provider anthropic_compat
  --output-dir artifacts/swebench_lite_dev_live_r10
  --work-root artifacts/swebench_repos
  --skip-clone --skip-verify
  --max-retries 3
  --repair-timeout-s 900
```

| 项 | 值 |
|----|-----|
| Manifest | `skip_verify: true`；`repair_mode: patcher_primary`；`localize_retrieve: skipped`；commit `bb5f9b55…` |
| 进程 | shell exit **1**（PowerShell 把 stderr `[progress]` 标成 NativeCommandError）；adapter `"ok": true` |
| 汇总 | `failure_summary`: **none=4 / agent=1**；verified 全 false（预期：跳过 verify） |
| 墙钟 | ~20 min（`elapsed_ms≈1.18e6`） |

日志：`artifacts/swebench_lite_dev_live_r10/run.log`
进度：`artifacts/swebench_lite_dev_live_r10/progress.jsonl`
报告：`artifacts/swebench_lite_dev_live_r10/adapter_report.json`
预测：`artifacts/swebench_lite_dev_live_r10/predictions.jsonl`

---

## 1. 五题总览

| instance_id | repair_status | patch_B | verified | failure_detail | 关键信号 | duration_ms | run_id |
|-------------|---------------|---------|----------|----------------|----------|-------------|--------|
| astropy__astropy-12907 | failed | **0** | false | `empty_model_patch` | `apply_failed`；`apply_ok=0`；`writes_used=0`；Loc/Ret **0ms** | 477034 | `730af971-…` |
| django__django-11099 | fixed | **747** | false | `pending_verify` | seed `allowed_edit=0`；`apply_ok=0`；导出补丁 **类定义被撕裂** | 115817 | `686ffc99-…` |
| matplotlib__matplotlib-23964 | fixed | **432** | false | `pending_verify` | seed `allowed_edit=0`；**`apply_ok=1`**；补丁观感相对干净 | 97775 | `7e884811-…` |
| pylint-dev__pylint-6506 | fixed | **255** | false | `pending_verify` | seed lock=6；`apply_ok=0`；重复 `sys.exit(1)` | 87671 | `4ecc54e4-…` |
| sympy__sympy-20590 | fixed | **280** | false | `pending_verify` | seed lock=6；`apply_ok=0`；hunk 行号/上下文 **像截断片段** | 387768 | `d4952025-…` |

共性（progress / report）：

- `localize_ms=0`、`retrieve_ms=0`（primary 跳过 Loc/Ret LLM ✅）
- 四题 nonempty 均标 `fixed` + `pending_verify`（**未跑权威 Verifier**）
- 仅 matplotlib 在 progress 里出现 `apply_ok=1`；其余 nonempty 题 `apply_ok=0`（更可能走旧 write/legacy apply，而非稳定 `apply_patch` 主路径）

### 相对近期基线（管道面）

| 项 | R6（开 verify） | R10（skip-verify + primary） | 解读 |
|----|-----------------|------------------------------|------|
| nonempty | 3/5 | **4/5** | 产出面上升；质量未验证 |
| agent 空 patch | astropy+pylint | **仅 astropy** | pylint 本轮有导出，但补丁可疑 |
| Loc/Ret | llm→degrade | **skipped / 0ms** | primary 主目标达成 |
| verified | 0 | 0（跳过） | 本轮不评 verified |
| 墙钟 | 较长（含 verify） | ~20min 五题 | 跳过 verify + 无 Loc/Ret 加速明显 |

---

## 2. 问题类卡片（R10）

### P1 — `apply_patch` / apply 空 preimage → 零写入（astropy）

| 字段 | 内容 |
|------|------|
| class_id | `R10_apply_empty_original`（近亲 R6 `E6a_apply_or_sibling` / parse 空） |
| symptom | `failure_tags=["apply_failed"]`；`model_patch=""`；`candidate_patches=[]`；progress `apply_ok=0` |
| affected_count | 1（astropy） |
| fix_level | 强制 `Update File` 携带可读 preimage；工具层拒绝 empty_original；失败回灌须含 near= 与「先 read 再 apply」；legacy recovery 在 parse empty 时勿空转耗预算 |
| generic_rule | 解析成功 ≠ 写入成功；empty_original 应计 tool reject 并缩短无效 turn |
| anti_overfit_check | 任意仓：故意空 original 的 apply → 明确错误码，不静默 0 字节 |

**证据**

```text
# run.log
[patcher] ⚠ 无法应用补丁: astropy/modeling/separable.py
  (hunk_mismatch:astropy/modeling/separable.py:empty_original)
[patcher] 补丁解析成功但未写入任何文件
[patcher] apply recovery 1: parse empty
[patcher] apply recovery 2: parse empty
repair_finished: status=failed total_ms=461798
instance … class=agent patch_bytes=0

# report.json (runs/730af971-…)
failure_tags: ["apply_failed"]
phases: localize_ms=0 retrieve_ms=0 patch_ms=188035 repair_total_ms=461798
runtime_metrics: tool_steps=12 writes_used=0
repair_plan.suspect_files: separable.py + test_separable.py  # 定位其实对

# adapter_report
failure_class=agent failure_detail=empty_model_patch
```

`empty_original` 定义见 `src/repair/patch_applier.py`：`original_lines` 空且无可用 diff 匹配时返回该码。

---

### P2 — 规则种子 `allowed_edit` 过噪或为空

| 字段 | 内容 |
|------|------|
| class_id | `R10_seed_lock_noise_or_empty` |
| symptom | F2P 合理，但 lock 含大量无关文件；或 `allowed_edit=[]` 仍标 fixed |
| affected_count | astropy（噪）；django/matplotlib（空锁仍产出） |
| fix_level | seed 优先：F2P 文件 → 同包实现 `.py`；限制 N；空锁时应扩大/告警而非默默放行任意写 |
| generic_rule | lock 是安全边界也是注意力边界；空锁 = 失控面 |
| anti_overfit_check | 合成多包仓：仅 F2P 邻域进 lock，无关包不得进 |

**证据**

```json
# progress.jsonl — astropy seed_ready
"allowed_edit": [
  "astropy/constants/constant.py",
  "astropy/convolution/core.py",
  "astropy/convolution/kernels.py",
  "astropy/cosmology/core.py",
  "astropy/modeling/functional_models.py",
  "astropy/modeling/polynomial.py",
  "astropy/modeling/separable.py",   // 真相关
  "astropy/units/quantity.py"
],
"f2p": [
  "astropy/modeling/tests/test_separable.py::test_separable[compound_model6-result6]",
  "astropy/modeling/tests/test_separable.py::test_separable[compound_model9-result9]"
]

# django / matplotlib
"allowed_edit": [], "suspects=0"
# 但仍 repair_finished status=fixed + nonempty predictions
```

---

### P3 — Primary 路径仍冷加载语义模型（HF）

| 字段 | 内容 |
|------|------|
| class_id | `R10_semantic_model_on_primary` |
| symptom | 首题 patcher_turn 打印「加载语义模型 (~90MB)」；HF `ReadTimeout` 后重试成功 |
| affected_count | 至少首题（全局单例后续题受益） |
| fix_level | patcher_primary / repair 工具环默认不触达 `semantic_memory`；或显式 `FIXLOOP_SEMANTIC=0`；预下载/离线缓存 |
| generic_rule | 跳过 Loc/Ret ≠ 跳过 embedding 副作用；懒加载属性访问即付费 |
| anti_overfit_check | primary 单测：构造 AgentRuntime 跑一轮工具，不出现 HF/语义模型 I/O |

**证据**

```text
# run.log（astropy 首 turn）
[agent_runtime] 加载语义模型 (~90MB)...
ReadTimeoutError ... huggingface.co ... all-MiniLM-L6-v2 ... Retrying in 1s
 ✅
```

实现入口：`agent_runtime/runtime.py` → `semantic_memory` 属性；`features/memory/semantic.py` 加载文案。

---

### P4 — skip-verify 下「fixed」掩盖坏补丁（质量债）

| 字段 | 内容 |
|------|------|
| class_id | `R10_pending_verify_junk_patch`（近亲 `E20` / `E15 pending_verify`） |
| symptom | `repair_status=fixed` + `failure_detail=pending_verify` + 导出补丁语法/语义明显坏 |
| affected_count | 3+（django、pylint、sympy；mpl 待开 verify 再判） |
| fix_level | Critic 增强：语法 compile / 明显重复行 / hunk 行号离谱；**下一轮必须开 verify** 才能谈 verified KPI |
| generic_rule | nonempty ≠ 正确；skip-verify 只服务管道吞吐，不服务得分叙事 |
| anti_overfit_check | 合成「撕裂 class 头」的 diff → Critic reject，不得标 fixed |

**证据（predictions.jsonl 摘录）**

django — 把 `@deconstructible` 下的缩进行当成「删除」，再「新增」`class …`（类定义结构被撕）：

```diff
 @deconstructible
-    regex = r'\A[\w.@+-]+\Z'
+class ASCIIUsernameValidator(validators.RegexValidator):
     regex = r'\A[\w.@+-]+\Z'
```

pylint — `KeyboardInterrupt` 与 `_UnrecognizedOptionError` 分支出现重复 `sys.exit(1)`：

```diff
     except KeyboardInterrupt:
+        sys.exit(1)
     except _UnrecognizedOptionError:
-        sys.exit(1)
         sys.exit(1)
```

sympy — 导出 hunk 以 `@@ -1,3 +1,4 @@` 起手，正文却是缩进后的 `def diff`；而仓库内 `def diff` 实际约在 **L194**（`sympy/algebras/quaternion.py`），强烈暗示 **上下文/行号切片错误** 或脏片段导出：

```diff
--- a/sympy/algebras/quaternion.py
+++ b/sympy/algebras/quaternion.py
@@ -1,3 +1,4 @@
     def diff(self, *symbols, **kwargs):
+        kwargs = dict(kwargs)
         kwargs.setdefault('evaluate', True)
```

---

### P5 — `apply_patch` 主路径采用率低

| 字段 | 内容 |
|------|------|
| class_id | `R10_apply_patch_low_adoption` |
| symptom | progress `tool_progress: apply_ok=0` 但仍 nonempty（django/pylint/sympy） |
| affected_count | 3 |
| fix_level | 工具策略：写文件主推 `apply_patch`；限制裸 `write`；计数写入 report；面试/指标看 `apply_patch_ok_count` |
| generic_rule | 新编辑面未成为默认成功路径时，Phase B 收益不可宣称 |
| anti_overfit_check | 单测：canonical repair 工具列表含 apply_patch；集成测断言至少一次 ok 或明确 reject |

**证据**

```text
# progress.jsonl / run.log
django:     apply_ok=0  → patch_bytes=747
matplotlib: apply_ok=1  → patch_bytes=432   # 唯一工具层成功样例
pylint:     apply_ok=0  → patch_bytes=255
sympy:      apply_ok=0  → patch_bytes=280
astropy:    apply_ok=0  → patch_bytes=0
```

---

### P6 — 跑批外壳噪声（非 Agent 逻辑）

| 字段 | 内容 |
|------|------|
| class_id | `R10_powershell_stderr_exit` |
| symptom | `python -m … 2>&1 \| Tee-Object` 下 stderr progress → `NativeCommandError`；进程 exit 1，但 adapter `ok: true` |
| fix_level | 进度改 stdout，或 PS 侧 `$ErrorActionPreference` / 仅重定向 stdout；文档注明勿用 Tee 误判 |

**证据**：`run.log` 开头 `CategoryInfo: NotSpecified … NativeCommandError`；文末仍打印 `ok: true` 与三路径。

---

## 3. 已验证达成（本轮）

| 项 | 观察 |
|----|------|
| Loc/Ret 跳过 | 五题 `localize_ms=retrieve_ms=0`；manifest `localize_retrieve=skipped` |
| Progress / heartbeat | `repair_started`→`seed_ready`→`patcher_turn`→`tool_progress`/`apply_patch_span`→`repair_finished` 齐全 |
| nonempty 吞吐 | 4/5；墙钟显著低于含 verify 的旧轮次 |
| matplotlib 工具写 | `apply_ok=1` 证明 `apply_patch` 路径可通 |

---

## 4. 建议修复顺序（问题类，不特判题）

| 顺序 | class_id | 理由 | 状态（方案 B / `bonus/patcher-primary-swe`） |
|------|----------|------|-----------------------------------------------|
| 1 | **P1** `R10_apply_empty_original` | 直接制造 agent 空 patch；与 Phase B 主编辑面相关 | **已修**：parse/tool 拒 empty preimage；recovery parse empty 早停 |
| 2 | **P5** `R10_apply_patch_low_adoption` | 否则 nonempty 质量与新工具脱节 | **已修**：prompt/工具描述/quota 对齐 apply_patch |
| 3 | **P4** Critic/语法门 + **下一轮开 verify** | 清掉 django/pylint/sympy 垃圾「fixed」 | **已修** Critic junk/syntax；**仍待** 开 verify 跑批 |
| 4 | **P2** seed lock 收紧/空锁策略 | 降噪 + 防失控写 | **已修并加固**：F2P 强制置顶进锁；`lock_reflect` 错锁可扩 |
| 5 | **P3** primary 禁用语义模型冷启动 | 稳首题延迟与离线可复现 | **已修**：primary / `FIXLOOP_SEMANTIC=0` 跳过 precedent 语义 |
| 6 | **P6** 跑批脚本 | 避免假 exit 1 | **已修**：progress 默认 stdout；心跳默认不刷屏 |
| 7 | **R11** 错锁无反思 / 工具空转 | 锁错→零工具/`parse_fail`；只读不写撞 step_limit | **已修**：`lock_reflect` 重试；patcher stall 计读步；拒 shell 回灌 |

---

## 5. 证据索引

| 路径 | 用途 |
|------|------|
| `artifacts/swebench_lite_dev_live_r10/manifest.json` | 模式/预算/skip_verify |
| `artifacts/swebench_lite_dev_live_r10/adapter_report.json` | 五题 failure_class / duration / run_id |
| `artifacts/swebench_lite_dev_live_r10/predictions.jsonl` | 导出补丁原文 |
| `artifacts/swebench_lite_dev_live_r10/run.log` | apply 失败与语义模型超时 |
| `artifacts/swebench_lite_dev_live_r10/progress.jsonl` | seed / apply_ok / 阶段事件 |
| `artifacts/swebench_repos/<id>/.agent/runs/<run_id>/report.json` | phases、failure_tags、token/tool |
| `artifacts/swebench_repos/<id>/.agent/repairs/<run_id>/repair_state.json` | status、candidate_patches |
| `artifacts/swebench_repos/<id>/.agent/runs/<run_id>/trace.jsonl` | span（含 `apply_patch_span`） |

---

## 6. 下一轮跑法建议

```text
# R11：同 DEV5 + patcher_primary，打开 FixLoop verify（勿 skip-verify）
# 先修 P1/P5 最小补丁后再跑，否则仍易刷 nonempty、刷不出 verified
python -m src.benchmark.swebench \
  --provider anthropic_compat \
  --output-dir artifacts/swebench_lite_dev_live_r11 \
  --work-root artifacts/swebench_repos \
  --skip-clone \
  --max-retries 3 \
  --repair-timeout-s 900
```
