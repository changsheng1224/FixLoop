# Patcher Primary：SWE-bench 主环改造方案

> 状态：草案（待确认后实现）
> 日期：2026-08-06（Critic + SWE-agent ACI + Codex + Claude Code + **Cursor**；**primary 路径移除 Localizer/Retriever**）
> 背景：DEV5 R8–R10 显示瓶颈在「编辑落地 / 文件锁定 / 预算错配 / 超时拖尾」，而非再增加 Localizer/Retriever 能力。
> 对标：SWE-agent（ACI）+ Codex（`apply_patch`）+ Claude Code（搜读改测同环）+ Cursor（读结果再决策）。
> 约束：问题类通用修复；禁止 instance ID / gold patch 特判（见 `docs/SWE_BENCH_LITE_FIX_CONSTRAINTS.md`）。
> Claude Code 裁决：见 §5.3（编辑主面 **方案 A = `apply_patch` 优先**）。
> Cursor 裁决（2026-08-06）：见 §5.4；除「读结果再决策」外其余保持现稿。
> **进度可感知**（2026-08-06）：运行中向用户展示阶段性进度输出，见 §4.9。

---

## 1. 目标与非目标

### 1.1 目标

1. **SWE / repair 默认路径**改为：规则定位种子 → **Patcher 长环（搜+读+改）** → **Critic 轻审** → **Verifier（环外）**。
2. **`patcher_primary` 路径移除 Localizer / Retriever 调用**（搜读工作并入 Patcher；定位先验仅规则种子）。
3. 保留有价值的 multi 面：**Critic（评审）+ Verifier（判定）+ 停损**；去掉 Loc∥Ret 流水线交接。
4. 主环对齐 Codex：**以落盘 diff 为真源**、**结构化 `apply_patch` 优先于大 JSON 候选**、**测失败热回灌同对话**。
5. 主环对齐 Claude Code：**gather→act→verify 同环交织**、**未 Read 不可写**、**只读可并行 / 写串行**、**先 F2P/红测再广改**、**compact 序 + thrash 停**、同环 checklist。
6. 主环对齐 Cursor：**每步工具/测试结果必须进入下一决策**（读结果再决策）；**不**恢复语义 Ret、**不**做 Mode/Plan/Debug/checkpoint/无限步。
7. 先抬高 **非空 `model_patch`**，再追 **内部 `verified=true`**（本阶段不以官方 harness `resolved` 为唯一 KPI）。
8. **面试可演示**：规则种子 → Patcher/`apply_patch`+ACI → Critic → Verifier 回灌，trace 可观测。
9. **运行进度可感知**：repair / SWE 跑批过程中，向用户（CLI / 日志 / 可选 SSE）持续输出**阶段性进度信息**，避免长时间「无输出黑盒」；进度事件与 Canonical Trace 同源或可对齐。

### 1.2 非目标

- **不**在 Phase A 物理删除 `create_localizer` / `create_retriever` 源码（`pipeline` 模式暂留对照）；primary 路径 **零调用**。
- **不**用 LLM Critic/「投票」替代 pytest/sandbox Verifier（Critic 只做提交前廉价过滤）。
- 不把 Verifier 改成对话式 Agent。
- 不做整仓「一夜单 Agent」重构 Layer1；**不**绑定 OpenAI Responses API / Codex CLI 运行时。
- **不**开放无约束裸 shell 作为唯一编辑面（Codex/CC 可走 shell；FixLoop 仍用受限工具 + `apply_patch`）。
- **不**把编辑主面改成 Claude Code 式 `Edit` 优先（裁决：**方案 A**，`apply_patch` 为主；精确 replace 仅兜底）。
- **不**做交互 Plan mode / 前 N turn 只读软预算 / 每次 edit 文件 checkpoint（沿用超时 salvage）；**不**在 Patcher 内开 explore 子 Agent。
- **不**恢复 Cursor 式语义 codebase search / Retriever（primary 仍零 Ret）；**不**做 Ask/Plan/Debug Mode 产品面、HITL diff 审、Rules 引擎、Debug 插桩环、云端/Best-of-N、排队插话、无限 tool calls。
- **不**在 primary 中恢复 Localizer∥Retriever，也不保留「弱锚再请 Localizer」旁路（搜读一律 Patcher）。
- **不**做完整 TUI Dashboard / 实时 diff 可视化编辑器；进度输出以 **结构化阶段事件 + 短摘要** 为主（见 §4.9）。
- **不**默认把模型完整思维链 / 全量 tool 输出刷屏；用户面只展示进度摘要，细节仍进 Trace。

### 1.3 成功指标（DEV5 同配置对照）

| 指标 | 基线参考 | 第一验收 | 第二验收 |
|------|----------|----------|----------|
| nonempty patch | R9=1/5；R10 已见 django 非空 | **≥3/5** | ≥4/5 |
| `verified` | R4–R9 = 0 | ≥0（不退步） | **≥1/5** |
| 单实例墙钟 | astropy R10 ~1224s（超时拖尾） | ≤ `repair_timeout_s` + 30s | 同左 |
| 空转 | parse_thrash 烧满 900s | parse_fail 连续 2 次必须换策略 | 同左 |
| Critic | — | 空/越界 diff 在进沙箱前被拒，trace 可见 | 误伤率可接受（不系统性清空金标文件 diff） |
| 编辑路径 | JSON/loose recover 主导 | Phase B：`apply_patch` 成功占比可观测 | 同左 |
| 进度感知 | 长跑常无阶段性输出 | CLI/日志可见阶段心跳（种子/turn/工具/Critic/Verify） | 面试演示无「黑屏等 10 分钟」 |

对比实验必须固定：模型、timeout、max_retries、DEV5 manifest、sandbox 开关、`repair_mode`。

---

## 2. 问题陈述（为何要改）

### 2.1 现状流水线

```
Issue → parse/plan → Localizer ∥ Retriever → tier gate
     → Patcher(retry) → Verifier → feedback → …
```

SWE 上常见实际形态：

- Localizer **规则跳过 LLM**，Retriever **降级**，预算仍被探索/交接吃掉。
- Patcher 只吃压缩后的 suspect/feedback，**不能完整继承搜读轨迹**。
- 短修可能 **改偏**（例：astropy 已确认 `separable.py`，短修却指向无关模块）。
- 闸门与恢复路径多 → 易「空更安全」；超时 Future 触发后 worker 仍跑 → 墙钟超标且 diff 易丢。
- 垃圾 / 越界 diff 直接进沙箱 → 贵、慢，且面试上缺少「执行 vs 评审」分工。
- 编辑真源偏 **消息里的 CandidatePatch JSON**，与 Codex「先落盘、再从工作区出 diff」相反 → parse/apply 空转多。

### 2.2 与成功单 Agent 的差距（要点）

成功的 SWE / Codex / Claude Code / Cursor 类系统：**一个长环 + 稳编辑落盘 + 热测试反馈 + 读结果再决策 + 可控上下文**。
FixLoop 缺的不是「更多角色」，而是 **主闭环对准正确文件并持续有效尝试**。
Critic 补的是：**主环之外的廉价第二意见**，不是第二套主环。
Localizer/Retriever 在 SWE 上边际低、交接损耗高 → **primary 路径移除，改由规则种子 + Patcher 工具环（Grep/Read）承担**。

---

## 3. 目标架构

### 3.1 总览

```
Issue
  │
  ├─ [轻量前置 · 非 LLM / 非 Loc·Ret]
  │     规则定位种子
  │     (F2P / test_patch / stack / issue path / plan 路径)
  │     → 写入 allowed_edit / suspect 种子
  │
  ▼
┌──────────────────────────────────────────────────────────┐
│  Patcher = Primary Repair Agent（主预算）                  │
│  心智：gather → act → verify 同环交织（Claude Code）         │
│  turn: model → tool(s) → **读结果再决策** → … 直至提交/停损 │
│  搜读：grep/glob/read（只读可并行；窗口化；CC 序 compact）   │
│  编辑：apply_patch 优先（裁决 A）+ ACI；未 Read 不可写       │
│  约束：writable ⊆ allowed_edit；可 expand_lock；写串行       │
│  内环：先 F2P/红测 → 搜读改 → 快检 → 再改；真源=工作区 diff  │
│  checklist：同环短清单（默认启用，不占独立阶段）             │
└──────────────────────────────────────────────────────────┘
  │ 提交：从磁盘/登记 diff 导出 candidate（非「最后一条 JSON」）
  ▼
┌──────────────────────────────────────────────┐
│  Critic = 轻量评审（短调用，可规则优先）          │
│  只答：空？越锁？只改测试？明显无效？             │
│  reject → 回灌 Patcher；accept → 进 Verifier     │
└──────────────────────────────────────────────┘
  │ accept
  ▼
Verifier（sandbox / pytest，非 LLM）
  │ 失败摘要结构化回灌主环（下一 retry / 下一 turn）

并行：ProgressEmitter（§4.9）
  → CLI/stderr 阶段摘要 + Trace span（可选 progress.jsonl）
```

### 3.2 规则定位种子（替代 Localizer Agent）

不调用 Localizer LLM，用确定性规则生成初始可编辑文件集合，例如：

| 来源 | 模块/机制（既有） |
|------|-------------------|
| 官方 `test_patch` 路径与 import | `suspects_from_test_patch` |
| F2P / FAIL_TO_PASS hint | `suspects_from_fail_to_pass` |
| 栈帧、issue 内路径 | issue/stack 解析 |
| `RepairPlan.suspect_files` | `_parse_issue` / planner 规则结果 |

种子为空时：**不**回退 Localizer；由 Patcher 在锁定放宽策略下自行 grep（`expand_lock` 限额仍生效），Critic 挡越锁胡改。

### 3.3 角色职责矩阵

| 组件 | `patcher_primary` | 职责 | 预算 |
|------|-------------------|------|------|
| **规则种子** | **必跑** | 无 LLM 定位先验 → `allowed_edit` | 近零 |
| **Patcher** | **主 Agent** | 同环 gather/act/verify：搜、读、`apply_patch`、快检；承接原 Loc/Ret | ≥80% 墙钟与 steps |
| **Critic** | 默认开 | 提交前过滤空/越界/纯测试 diff（≈ Codex 写路径策略的第二道） | ≤15s 或纯规则 |
| **Verifier** | 必跑 | 权威 `verified` + 结构化回灌 | verify 阶段预算 |
| **Localizer** | **移除（不调用）** | — | 0 |
| **Retriever** | **移除（不调用）** | — | 0 |
| **Stop-loss / Blackboard** | 保留 | 停损、锁定、拒绝原因、compact 触发 | 非 LLM |

`FIXLOOP_REPAIR_MODE=pipeline`：仍可走旧 Loc∥Ret→Patcher，供对照与回归；**非本方案交付主路径**。

### 3.4 模式开关

```text
FIXLOOP_REPAIR_MODE=patcher_primary   # SWE 默认：无 Loc/Ret
FIXLOOP_REPAIR_MODE=pipeline          # 旧路径（暂留）

FIXLOOP_CRITIC=1
FIXLOOP_CRITIC_MODE=rules_first       # rules_first | llm | off

FIXLOOP_APPLY_PATCH_PRIMARY=1         # Phase B：主编辑面优先 apply_patch
FIXLOOP_PATCHER_COMPACT=1             # Phase B/C：长环工具史压缩
```

- SWE adapter 强制或默认 `patcher_primary`。
- primary 下 `self.localizer` / `self.retriever` 可不构造，或构造但不调用；**禁止** `_run_localize_and_retrieve`。
- Critic LLM 失败 → accept + `critic_skipped`。

---

## 4. 主环行为规范

### 4.1 文件锁定（File Lock）≈ Codex writable-path safety

允许编辑集合 `allowed_edit` 初始为：

1. 规则种子：test_patch 反推 impl、F2P 覆盖、栈帧、issue 路径
2. 主环 **已成功 read** 且通过 `is_file()` 的实现 `.py`（可上限 N=5）
3. （沿用既有）本轮 `suspect_locations` 中的 HIGH/MID 实现路径（若有）

硬约束（对齐 Codex writable paths + Claude Code Read-before-edit；自动化 SWE 无 HITL）：

- **未成功 Read 的路径默认不可写**（写入 `allowed_edit` 的「已读集合」后才可 `apply_patch`）；规则种子路径可先自动预读或首写前强制 read。
- **apply 前**校验：patch 内所有路径 ⊆ `allowed_edit`（归一化、防 `..`）；越界 → **Reject**，工具返回可读原因（不必等 Critic）。
- 写工具若目标 ∉ `allowed_edit` → 拒绝并提示「先 read / 先从种子扩」；或要求显式 `expand_lock`（计次，默认最多 2 次）。
- apply/精确替换上下文 **stale / 未匹配** → reject + `near=`（对齐 CC Edit staleness 精神，不引入交互 rewind）。
- **禁止**再用「与种子无关的短修 top-k」覆盖锁定集（修复 astropy 改偏类问题）。

### 4.2 编辑落地 — `apply_patch` 优先（裁决 A）+ ACI

**编辑主面裁决（相对 Claude Code `Edit` 优先）：选 A** — 仍以 **`apply_patch` 为主**；精确 `str_replace`/旧 edit 仅作兜底，**不**改为 Edit 双主面。

**真源顺序：**

1. **`apply_patch` 落盘**（首选）
2. 从工作区快照导出 unified diff → candidate / `model_patch`
3. 仅当落盘路径失败时：一轮精确 replace / JSON / loose recover（限时）
4. 仍空 → **记 parse_fail，触发策略切换**，不无限 grounded retry 烧超时

`apply_patch` 行为规范（参考 Codex / GPT apply_patch，落地到既有 `patch_engine` / tools）：

| 要求 | FixLoop 落地 |
|------|----------------|
| LLM 友好补丁格式 | 支持 `*** Begin/End Patch` + `*** Update/Add/Delete File` + `@@` 上下文；或沿用 unified hunk；**鼓励 freeform，少嵌套 JSON** |
| 宽松解析 | Lenient：剥 heredoc/markdown 围栏；失败返回**语法级**错误供下一 turn 自纠 |
| 应用结果必回灌 | 成功：`ok` + 写后窗口；失败：`reject` + `near=` / stale / 未匹配（禁止 silent 空成功） |
| 路径安全 + 已读 | apply 前 ⊆ `allowed_edit` 且路径已 Read（§4.1） |
| 语法不过不落盘 | `.py` → edit-time lint；失败拒绝写入（ACI；对齐 CC 改后可见失败） |
| 导出真源 | Critic/Verifier/export **优先吃磁盘 diff**，不以「最后一条 assistant JSON」为准 |

与 P0/P1 已做能力对齐：faithfulness soft、路径后缀解析、超时 salvage、suspect-scoped 导出。

**SWE-agent ACI 硬要求（与上表并存，详见 §5）：**

| 要求 | FixLoop 落地 |
|------|----------------|
| 改完立刻可见 | 成功后工具返回 **更新后的窗口片段**（不必再 read） |
| Critic ≠ lint ≠ path-safety | **path-safety/已读** 挡越锁盲写；**lint** 挡语法脏写入；**Critic** 挡空/纯测试等提交级问题 |

### 4.3 Critic（新增）

#### 职责边界

| Critic 做 | Critic 不做 |
|-----------|-------------|
| 空 patch / 无 unified diff | 判断能否通过官方测试 |
| 改动路径 ⊈ `allowed_edit`（越锁；双保险） | 重写补丁内容 |
| 仅测试文件、无实现文件 | 替代 Verifier / 替代 apply 前 path-safety |
| 可选：diff 过大（文件数/字节超阈）打软警告 | 长工具环探索仓库 |

#### 两级实现（推荐 `rules_first`）

1. **Rules（默认必跑，零 LLM）**
   - `looks_like_unified_diff` / 非空
   - `patch_paths ⊆ allowed_edit`（归一化后）
   - 至少 1 个非测试实现文件（除非 issue 明确为 test-only，本阶段 SWE 默认要求有 impl）
   - 文件数 ≤ `MAX_EXPORT_FILES`、字节 ≤ 软阈（与导出闸门对齐）
2. **LLM（可选，`FIXLOOP_CRITIC_MODE=llm`）**
   - 输入：锁定集 + diff 摘要（≤4KB）+ 失败回灌（若有）
   - 输出 JSON：`{ "verdict": "accept"|"reject", "reasons": [...], "hint": "..." }`
   - `complete_once`，超时 ≤15s；失败则 accept + `critic_skipped`

#### 与流水线衔接

```
Patcher 产出 candidate（磁盘真源）
  → Critic.review(diff, allowed_edit, state)
  → reject: 不写 Verifier；reasons/hint 写入 feedback；retry_count 策略由 stop_loss 决定
  → accept: 进入 Verifier
```

- Critic reject **不计**为 `verified` 失败，计为 `critic_rejected`（node_timings + failure 元数据）。
- 连续 Critic reject ×2 → 允许一次 `expand_lock` 或缩到单文件，避免死循环。
- Trace 事件：`critic_started` / `critic_finished`（含 verdict、mode=rules|llm、reasons）。

#### 面试话术锚点

> 「Patcher 同环搜读改测（对齐 Claude Code gather/act/verify，编辑面用 Codex `apply_patch`）；Critic 是廉价闸门；Verifier 才是沙箱实测。执行、评审、判定三权分离。」

### 4.4 读/搜/动作纪律（ACI + Codex + Claude Code + Cursor）

0. **读结果再决策（采纳 Cursor）**：每次 tool / 快检 / apply 回执必须进入下一 turn 的可见上下文；禁止「开火后不管」或只记 node_timings 却不进 Patcher prompt。下一步动作须由**上一结果**驱动（失败则纠、成功则推进 checklist）。
1. **窗口化 read**：默认约 **100 行**窗口 + 可选 scroll/goto；禁止主环一次灌入数千行 snippet（抑制 R10 类 `n_snippets` 爆炸）。
2. **精简 grep/glob（对齐 CC，保持）**：全仓搜索默认返回 **命中文件列表**（+ 可选每文件 1～2 行预览）；**Grep 优先于 embedding Retriever**（primary 已去 Ret；**不**恢复 Cursor 语义搜）。
3. **空输出显式化**：工具成功但无内容时返回固定文案（如 `command succeeded with no output`），禁止空白字符串。
4. **只读可并行、写串行（采纳 CC）**：同一 turn 允许多个 Read/Grep/Glob；**每 turn 至多一次写**（`apply_patch` / edit）；少「一次吐多文件大 JSON」。
5. **上下文 compact（采纳 CC 序）**：
   - 先丢弃/摘要 **旧的成功 tool 输出**（尤其大段 read）；
   - **保留最近 K 次失败 apply / 快检 / Verifier 回灌原文**；
   - 种子路径与 checklist 状态永不丢；
   - **compact thrash**：连续 compact 后上下文仍立即爆满 → 停损（不再空转 compact），记 `compact_thrash`。
6. **硬停损优先于「无限 tool calls」**：学 Cursor 环结构，**不**学无上限；timeout / thrash / stop_loss 一律硬停。

### 4.5 环内快检 + 环外 Verifier（强化：先红测）

对齐 Claude Code「给可验证目标」、Codex「测到过」、SWE-agent「测在环内」：

| 层级 | 谁 | 作用 |
|------|-----|------|
| 快检 | Patcher 工具（**优先 F2P / 已知失败 nodeid**，再相关单文件） | 热反馈，进**同一对话**下一 turn |
| 权威 | Verifier（sandbox/pytest） | 决定 `verified`；结构化回灌主环 |

**强化纪律（采纳）：**

1. 有 F2P / fail_to_pass / 已知失败 nodeid 时：**优先跑这些**，再广搜/广改。
2. 规则种子应把 F2P nodeid 写入 Patcher 可见状态（prompt / blackboard）。
3. 无红测线索时：才允许「先 grep 定位 → 再改 → 再选测」。

Verifier / 快检失败后写入主环可见结构（blackboard / node_timings + prompt 段）：

```text
failed_nodeids: [...]
failure_excerpt: ≤2KB
hint_files: 从 nodeid / traceback 解析的路径
```

下一轮 Patcher prompt / 下一 turn **必须**带上上述字段；禁止只回灌模糊自然语言。

### 4.6 超时与取消

1. `repair_timeout_s` 触发后：先 **salvage disk→candidate**，再回滚未锁定变更（或保留已登记 patch）。
2. `cancel_token` 必须能打断/放弃后续 LLM 调用。
3. `ThreadPoolExecutor` 退出不得无界等待；目标：**墙钟 ≤ timeout + 30s**。
4. phase budget：primary **无 localize/retrieve 阶段**；patch 吃满预留；Critic ≤15s 挤在 patch/verify 间隙。
5. **不做**每次 edit 文件 checkpoint / 交互 rewind（裁决：保持现有 salvage）。

### 4.7 停损与策略

| 信号 | 动作 |
|------|------|
| parse_fail / apply_reject ×2 | 缩到单文件锁定 + 强制 `apply_patch` 短格式；禁止再开长 tools 环空转 |
| apply_fail ×2 | 换 edit 模式或换锁定文件（避开本轮已连续失败的路径） |
| critic_reject ×2 | expand_lock 一次或强制单文件；写入 hint |
| verify env ×2 | stop_loss env（既有） |
| 零增益 ×2 | 允许一次 `expand_lock`，否则 exhausted |
| context 膨胀 | 触发 §4.4 compact 序 |
| compact_thrash | 停止 compact 空转；缩 read 窗口 / 强制提交或 exhausted |

### 4.8 同环 checklist（采纳；非独立 Planner）

不恢复独立 Planner Agent；**不**做 Claude Code 交互 Plan mode /「前 N turn 只读」软预算（裁决：保持不做）。

Patcher **默认**维护短 checklist（≤5 条，可由规则种子预填，运行中整表更新，对齐 CC Todo 精神）：

```text
1. 跑/看 F2P 红测（若有）
2. 定位对应 impl（grep/read）
3. apply_patch
4. 快检
5. 提交 Critic
```

仅作 prompt 锚点与 trace；**不**单独占预算阶段。

### 4.9 运行进度可感知（用户面输出）

> **需求**：在 repair / SWE 批跑过程中，希望能显示一定的输出信息，供用户感知当前进度。
> **对标心智**：Claude Code / Cursor 类 Agent 长跑时有阶段感（在搜、在改、在测）；FixLoop 批跑与 CLI 同样需要，避免只剩最终 `report.json`。

#### 设计原则

1. **进度事件 ≠ 完整 Trace**：用户面是短、稳、可读的摘要；完整工具输出 / prompt 仍写 Canonical Trace（可脱敏）。
2. **与主环阶段对齐**：进度文案跟 primary 拓扑走，**不**再报 Loc/Ret 阶段（primary 路径）。
3. **同源**：进度回调与 `node_timings` / span 共用同一套阶段枚举，禁止两套互不一致的状态机。
4. **可关闭**：批跑默认开；`--quiet` / env 可关；CI 可只落日志文件。
5. **不阻塞主环**：emit 失败不影响 repair；禁止为刷进度额外打 LLM。

#### 必发进度事件（最小集）

| 时机 | 事件名（建议） | 用户可见摘要示例 |
|------|----------------|------------------|
| repair 开始 | `repair_started` | `repair start mode=patcher_primary timeout=900s` |
| 规则种子完成 | `seed_ready` | `seed: N files locked; F2P=…` |
| Patcher turn 开始/结束 | `patcher_turn` | `turn 3/… checklist: apply_patch` |
| 工具调用（聚合） | `tool_progress` | `grep → 12 hits` / `read foo.py:80-180` / `apply_patch ok` |
| 快检 | `quick_test` | `F2P fail (excerpt…)` / `F2P pass` |
| Critic | `critic_progress` | `critic reject: empty_diff` / `accept` |
| Verifier 开始/结束 | `verify_progress` | `verify running…` / `verified=false collect=0` |
| 停损 / 超时 / cancel | `stop_or_timeout` | `stop_loss: parse_fail×2` / `timeout salvage…` |
| repair 结束 | `repair_finished` | `status=exhausted patch_B=300 verified=false` |

可选增强（Phase C）：心跳 `heartbeat`（每 30–60s：当前 phase、已耗时、最近事件）；SWE runner 层 `instance_progress`（`2/5 django__… running`）。

#### 输出通道

| 通道 | 用途 | 落地 |
|------|------|------|
| **stderr / CLI 行日志** | 本地与面试演示默认 | `print`/`logging` 一行一文，带 `run_id` 短前缀 |
| **Trace / Langfuse span 属性** | 复盘与飞轮 | 同事件写 span；用户面可只 mirror 摘要 |
| **可选 SSE / JSONL progress 文件** | Web 或批跑仪表 | `.agent/runs/{id}/progress.jsonl` append-only |

#### 与「读结果再决策」的关系

- **模型侧**：工具完整 Observation 进下一 turn（§4.4.0）。
- **用户侧**：同一工具只 mirror **一行摘要**（工具名 + 成败 + 关键路径/计数）。
- 二者并行，互不替代。

#### 面试话术锚点

> 「主环跑很久时，用户不会对着黑屏干等：种子、每一 turn、apply、快检、Critic、Verify 都有进度行；细节在 Trace 里，演示时能边跑边讲卡在哪一拍。」

---

## 5. 对标借鉴：SWE-agent + Codex + Claude Code + Cursor

### 5.1 SWE-agent（ACI）

> 参考：[SWE-agent ACI](https://swe-agent.com/latest/background/aci/)、Yang et al.
> 结论：**接口设计往往比堆角色更能抬分**。

| 优先级 | SWE-agent 做法 | FixLoop 采纳 | 阶段 |
|--------|----------------|--------------|------|
| P0 | edit 后回显；语法错拒落盘 | edit-time lint + 写后窗口 | B |
| P0 | 空输出显式文案 | 工具网关统一文案 | A/B |
| P1 | ~100 行窗口 + scroll | 窗口化 `read_file` | B |
| P1 | 搜索短列表 | grep 摘要模式 | B |
| P1 | 环内跑测 | Patcher 快检 + 环外 Verifier；**先 F2P** | B |
| P2 | 一步一命令 | **写串行**；只读可并行（§5.3） | B |
| P2 | 步数/成本硬限制 | timeout + cancel（≤+30s） | C |

**不照搬**：裸 Linux shell ACI；无 Critic 的纯单 Agent 到底。

### 5.2 Codex（代码修复闭环）

> 参考：OpenAI Codex agent loop、`apply_patch`、writable-path safety、测后迭代、compact。

| 优先级 | Codex 做法 | FixLoop 采纳 | 阶段 |
|--------|------------|--------------|------|
| **P0** | **`apply_patch` 为编辑主面**（**裁决 A**） | 主环优先 `apply_patch`；精确 replace 兜底；**导出真源=磁盘** | **B** |
| **P0** | **writable path 在 apply 时拒绝** | §4.1 apply 前 ⊆ `allowed_edit` | A/B |
| **P0** | **单环 turn** | primary；同对话快检回灌 | A |
| P1 | 少并行写 | **每 turn 至多一次写**；只读并行见 §5.3 | B |
| P1 | Lenient patch 解析 | 宽松模式 + 自纠错误消息 | B |
| P1 | 测失败 → 同会话再改 | 快检 ≤2KB 进下一 turn；**先 F2P**（§4.5） | B |
| P1 | compact | 与 §5.3 CC 序合并落地 | B |
| P2 | 静态前缀可缓存 | system/tools 在前 | C |
| P2 | `update_plan` | §4.8 checklist **默认启用** | B |
| — | HITL / Responses API / 裸 shell 改文件 | **不采纳** | — |

### 5.3 Claude Code（代码修复逻辑）— 已裁决写入

> 参考：[How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works)（gather → take action → verify）；Grep/Read/Edit；只读并行；compact / thrash；Todo。
> **用户裁决（2026-08-06）**：1 采纳 · 2 **选 A** · 3 采纳 · 4 采纳 · 5 保持 · 6 **强化** · 7 采纳 · 8 采纳 · 9–15 **保持**。

| # | Claude Code | 裁决 | FixLoop 落地 |
|---|-------------|------|--------------|
| 1 | gather→act→verify 同环交织 | **采纳** | Patcher 内心智与 prompt/checklist |
| 2 | `Edit`（old→new）主面 | **选 A** | **仍 `apply_patch` 优先**；Edit/replace 兜底 |
| 3 | Read-before-edit / stale | **采纳** | 未 Read 不可写；失败 `near=`/stale |
| 4 | 只读并行、写串行 | **采纳** | §4.4 |
| 5 | Grep/Glob 一等、非 RAG | **保持** | primary 已去 Ret；grep 摘要 |
| 6 | 先有可验证目标 / 先红测 | **强化** | §4.5 优先 F2P nodeid |
| 7 | compact 序 + thrash 停 | **采纳** | §4.4 / §4.7 `compact_thrash` |
| 8 | Todo checklist | **采纳** | §4.8 **默认启用** |
| 9 | 交互 Plan mode / 前 N turn 只读 | **保持（不做）** | — |
| 10 | 每次 edit checkpoint / rewind | **保持（不做）** | 沿用超时 salvage |
| 11 | 改后诊断可见 | **保持** | edit-time lint + 写后回显 |
| 12 | explore 子 Agent 隔离上下文 | **保持（不做）** | compact + 窗口化 read |
| 13 | 权限/hooks vs Critic | **保持** | lock/lint 确定性闸 + Critic 提交审 |
| 14 | 磁盘真源 | **保持** | 与 Codex 一致 |
| 15 | HITL/会话记忆/MCP 全家桶 | **保持（不做）** | — |

### 5.4 Cursor（代码修复逻辑）— 已裁决写入

> 参考：[Cursor Agent](https://cursor.com/docs/agent/overview)、[Agent best practices](https://cursor.com/blog/agent-best-practices)（搜→改→跑命令→读结果；Rules；Plan/Debug Mode；语义搜；checkpoint）。
> **用户裁决（2026-08-06）**：3 **采纳**；其余 1–2、4–15 **保持**（不新增 Cursor 能力面）。

| # | Cursor | 裁决 | FixLoop 落地 |
|---|--------|------|--------------|
| 1 | Instructions+Tools+Model 按模型调 harness | **保持** | 不单开「按 provider 微调」任务 |
| 2 | Agent/Ask/Plan/Debug Mode 产品面 | **保持（不做）** | — |
| 3 | 搜→改→跑→**读结果再决策** | **采纳** | §4.4.0；工具/快检回执必进下一 turn |
| 4 | 边改边 apply + 人审 diff | **保持（不做 HITL）** | Critic 顶提交前廉价审 |
| 5 | 语义检索 + Grep | **保持** | **不恢复**语义 Ret；Grep+规则种子 |
| 6 | Rules / AGENTS.md 引擎 | **保持（不做）** | 固定短 system + 种子 |
| 7 | Plan mode | **保持（不做）** | §4.8 checklist 已覆盖轻量计划 |
| 8 | Debug 插桩复现环 | **保持（不做额外）** | 沿用既有 F2P/快检/Verifier 证据回灌 |
| 9 | Checkpoint / Restore | **保持（不做）** | 超时 salvage |
| 10 | 改后看 lint | **保持** | edit-time lint + 写后回显 |
| 11 | 只读并行等 | **保持** | 已按 CC 落地 |
| 12 | 云端/Bugbot/子 Agent 扩展 | **保持（不做）** | Critic 已有；无 explore 子 Agent |
| 13 | 排队插话 / 澄清问答 | **保持（不做）** | 批跑无用户 |
| 14 | 无死限 tool calls | **保持（拒绝）** | timeout + thrash 硬停损 |
| 15 | Browser/MCP/图像等 | **保持（不做）** | — |

### 5.5 分工防混淆

```
apply 当时   →  已读校验 + path-safety + lint + 写后回显
提交 Verifier 前 →  Critic rules/llm
权威判定     →  Verifier sandbox
```

面试可说：学 **Claude Code 的同环搜读改测**，学 **Codex 的 `apply_patch`**，学 **Cursor 的读结果再决策**，学 **SWE-agent 的 ACI**，用自有 **Critic/Verifier** 做评审与判定。

### 5.6 对 nonempty / verified 的预期作用

- `apply_patch` + 磁盘真源 + 未读不可写 + path-safety → 抬 **nonempty**
- 只读并行提高探仓密度；写串行降低脏写
- 先 F2P/红测 + 快检 + **读结果再决策** → 抬有效迭代，服务 **verified**
- CC 序 compact + thrash 停 → 抑制 R10 类上下文爆炸与超时拖尾
- 仅切 primary、不做上述编辑/compact → 改善有限

---

## 6. 模块改动清单（实现时）

| 模块 | 改动 | 预估 |
|------|------|------|
| `src/repair/pipeline.py` | `_repair_impl_patcher_primary`：不调用 Loc/Ret；规则种子→Patcher→Critic→Verifier；**提交吃磁盘 diff** | ~180–280 行 |
| `src/repair/critic.py`（新） | `review_patch`：rules + 可选 LLM；`CriticVerdict` | ~80–120 行 |
| `src/prompts/tasks/critic.md`（新） | 短评审任务模板（仅 llm 模式） | ~30–50 行 |
| `src/orchestrator.py` | primary 步数/超时；cancel；salvage；挂 Critic；跳过 localize_retrieve；**只读并行/写串行**；compact+thrash | ~100–160 行 |
| `src/agents/factory.py`（或等价） | primary 可不创建 loc/ret；Patcher 工具集 = patch+explore+`apply_patch` | ~40–80 行 |
| `agent_runtime/patch_engine.py` + tools | **lenient `apply_patch`**、**未读不可写**、path-safety、写后回显、lint | ~120–200 行 |
| 读/搜工具 | 窗口化 read、grep 摘要、空输出文案；支持同 turn 多只读 | ~80–120 行 |
| `src/prompts/tasks/patcher.md` + system | gather/act/verify；**读结果再决策**；**先 F2P**→读→`apply_patch`→快检；checklist；禁大 JSON | ~80–120 行 |
| `src/repair/short_repair.py` / fastpath | 仅规则种子驱动锁定；去掉对 Loc LLM 输出的依赖 | ~40–60 行 |
| `src/benchmark/swebench/*` | 默认 `patcher_primary`；manifest 记录无 loc/ret、`apply_patch`、F2P 快检计数 | ~20–40 行 |
| `src/repair/phase_clock.py` / adaptive | primary：无 localize/retrieve 预算 | ~20–40 行 |
| 上下文 compact（可挂 runtime） | **CC 序**：丢旧成功 tool 输出→留失败/快检；`compact_thrash` 停损 | ~80–120 行 |
| **进度输出（§4.9）** | `ProgressEmitter`：阶段事件 → CLI/stderr + optional `progress.jsonl`；与 span 同源枚举 | ~60–100 行 |
| `src/benchmark/swebench/runner.py` | instance 级进度行；透传 repair progress | ~20–40 行 |
| 测试 | 零 Loc/Ret、未读不可写、critic、`apply_patch`、F2P 优先、compact thrash、**进度事件最小集** | `tests/test_patcher_primary*.py` 等 |

旧 `pipeline` 路径保留；Critic 也可在后续挂到旧路径的 Patcher→Verifier 之间（可选，非 Phase A 必须）。

---

## 7. 分阶段落地

### Phase A — 切流骨架（先可跑）

- [x] `FIXLOOP_REPAIR_MODE` + SWE 默认 primary
- [x] **primary：不调用 Localizer/Retriever**；仅规则种子 → Patcher
- [x] Patcher 工具集含原 explore/grep/read（承接搜读）
- [x] **apply 前 path-safety** + **未 Read 不可写**（种子路径可预读）
- [x] F2P nodeid 注入 Patcher 可见状态（强化先红测的数据面）
- [x] **Critic rules_first**；工具空输出显式化；默认 checklist
- [x] 导出优先磁盘快照（若已有 tools diff，缩短 JSON 依赖）
- [x] **进度最小集（§4.9）**：`repair_started` / `seed_ready` / `patcher_turn` / `repair_finished` 打到 CLI（可 `--quiet`）
- [x] 单测：primary 路径无 `_run_localize` / retriever ask；未读拒写；进度事件可断言
- [ ] DEV5 冒烟：nonempty / 墙钟 / critic_rejected；**跑批时可见阶段行**

### Phase B — `apply_patch`（裁决 A）+ ACI + CC 纪律 + 快检

- [x] 文件锁定 + expand_lock
- [x] **`apply_patch` 优先**（lenient + stale/`near=`）+ edit-time lint + 写后回显
- [x] 窗口化 read + 精简 grep；**写串行**（多只读并行调度未做 ThreadPool）
- [x] **先 F2P/红测再广改**；环内 `quick_test` + Verifier `verify_progress`
- [x] 工具史 **CC 序 compact** + **`compact_thrash` 停损**
- [x] Critic reject 闭环（A）；parse/apply thrash 沿用 stop_loss
- [x] **进度补齐**：`tool_progress` / `critic_progress` / `verify_progress`
- [ ] DEV5：nonempty ≥3/5；报告 `apply_patch_ok_count`、`unread_write_reject_count`

### Phase C — 超时与面试可观测

- [x] cancel 硬停 + salvage（`repair_timeout.py`；`shutdown(wait=False)`；**无**每 edit checkpoint）
- [x] primary 阶段预算无 localize；既有 prefix cache 沿用
- [x] Critic/种子/`apply_patch` span + ProgressEmitter 对齐（**无 Loc/Ret span**）
- [x] **进度增强**：`heartbeat`、`FIXLOOP_PROGRESS_JSONL`；SWE `instance_progress`
- [ ] DEV5：墙钟达标；冲击 verified ≥1
- [x] 演示文档：`docs/PATCHER_PRIMARY_DEMO.md`；归档 pipeline Loc/Ret（非必须）待决策

---

## 8. 面试可演示的轨迹

1. **规则种子**（test_patch/F2P）→ `allowed_edit` + F2P nodeid（CLI：`seed_ready`）
2. **Patcher** checklist：先红测/看失败 → grep/read（可并行）→ **`apply_patch`**（须已读）→ lint/回显（CLI：`patcher_turn` / `tool_progress`）
3. **快检失败** → 同环再 patch（CLI：`quick_test`）
4. **Critic reject**（空/越锁）→ 回灌再改（CLI：`critic_progress`）
5. **Critic accept → Verifier fail → 结构化回灌 → Patcher**（CLI：`verify_progress`）
6. **全程**：旁观者不靠黑屏猜进度；需要细节时打开 Trace / `progress.jsonl`

口述：定位用规则 + Grep（对齐 Claude Code）；编辑用 `apply_patch`（裁决 A / Codex）；**每步读结果再决策**（Cursor）；搜改测在 **一个 Patcher 单环**；Critic 评审、Verifier 判定——**不再维护 Loc/Ret 双 LLM**；**运行中有阶段进度行，面试可边跑边讲**。

---

## 9. 风险与缓解

| 风险 | 缓解 |
|------|------|
| Patcher 无 Loc 后更盲改 | 规则种子；未读不可写；apply-time lock；Critic；expand_lock；精简 grep |
| 种子为空 | Patcher 自搜 + 扩锁上限；不回退 Localizer |
| 未读不可写过严 → 空转 | 种子路径自动预读；expand_lock 后须先 read |
| 只切 primary、不做 apply_patch/ACI | Phase B 绑定 nonempty；导出真源=磁盘 |
| `apply_patch` 格式模型不会 | lenient + 精确 replace 兜底；prompt 示例 |
| 先 F2P 在无测试环境失败 | 快检失败不阻断搜改；记 hint 继续 |
| Critic 过严 / 过松 | rules 可测；至少挡空 patch；越锁已在 apply 时挡 |
| Critic LLM 拖死超时 | 默认 rules_first；LLM ≤15s；失败 accept |
| edit-time lint 误伤 | 先仅 `.py`；`force` 计次上限 1 |
| compact 丢关键上下文 | 保留最近失败/快检；种子与 checklist 不丢；thrash 停 |
| 旧 pipeline 回归 | SWE 默认 primary；单测双模式 |
| 超时 salvage 脏 diff | scoped 导出（E12） |
| 照搬裸 shell / Edit 主面 / Plan mode / 子 Agent | 明确不采纳（§5.3） |
| 进度刷屏 / 泄露 prompt | 用户面仅摘要；完整输出进 Trace；`--quiet` 可关 |
| 进度与 Trace 状态不一致 | 共用阶段枚举；emit 失败不阻断主环 |

---

## 10. 明确不做

- Instance / repo 特判、读取 gold patch 调参。
- 用 Critic 或 LLM「语义正确」替代 Verifier。
- 用 Critic 替代 **edit-time lint** 或 **apply-time path-safety / 已读校验**。
- Critic 长环工具探索。
- 整仓改为 SWE-agent 裸 bash / Codex CLI / Claude Code 运行时依赖 / 删除 multi 角色。
- **以 Claude Code `Edit` 取代 `apply_patch` 主面**（已选 A）。
- 交互 Plan mode、前 N turn 只读软预算、每次 edit checkpoint/rewind、Patcher 内 explore 子 Agent。
- Cursor 式：语义 Retriever、Ask/Plan/Debug Mode、HITL diff、Rules 引擎、Debug 插桩环、云端/Best-of-N、排队插话、无限 tool calls。
- 同时大改 Skill Router / Intent 等无关子系统。
- 在 primary 未验收前继续加「空更安全」且不可观测的硬拦。
- 本改造范围内新做 Memorizer 角色 / 统一 repair memory API。
- **`patcher_primary` 中调用 Localizer/Retriever**（含弱锚一跳）；搜读只允许 Patcher + 规则种子。
- 恢复独立 Planner Agent 占预算（仅允许 §4.8 同环 checklist）。
- 完整 TUI Dashboard / 实时 diff 可视化编辑器（§4.9 仅结构化进度摘要）。
- 默认刷屏模型思维链或全量 tool Observation（细节进 Trace）。

---

## 11. 验收实验协议

1. **R_control**：`pipeline`，Critic off，同模型、900s、max_retries=3、DEV5。
2. **R_primary_A**：Phase A（primary + Critic rules + path-safety + 未读不可写 + F2P 注入 + **进度最小集**）。
3. **R_primary_B**：Phase B（+ `apply_patch` 优先 + ACI + 只读并行 + 先红测 + CC compact + **工具/快检/Critic/Verify 进度行**）。

报告字段：`repair_mode`、`critic_mode`、每实例 `patch_bytes`、`verified`、`critic_rejected_count`、`edit_lint_reject_count`、`apply_patch_ok_count`、`apply_path_reject_count`、`unread_write_reject_count`、`compact_thrash_count`、`repair_status`、`failure_detail`、墙钟、`phase_timeout_consumed_s`、**进度事件是否齐全（人工或日志断言）**。

主结论：**nonempty 与超时拖尾**；`verified` 为第二指标；**长跑可感知**为演示与批跑体验门槛。
消融建议：`primary` vs `primary+apply_patch` vs `+未读不可写/先F2P/compact`。

---

## 12. 决策摘要

| 问题 | 决定 |
|------|------|
| 是否单 Agent？ | **Patcher 主环（含搜读；gather/act/verify）**；Critic / Verifier / 停损保留 |
| Localizer/Retriever？ | **`patcher_primary` 路径移除（不调用）**；`pipeline` 暂留对照 |
| 规则种子？ | **保留且强化**，作为唯一无 LLM 定位先验 |
| SWE-agent？ | **采纳 ACI**；不照搬裸 shell |
| Codex？ | **采纳** `apply_patch` 主面（**裁决 A**）、磁盘真源、apply-time writable、同环测后迭代 |
| Claude Code？ | **采纳** 同环三拍、未读不可写、只读并行/写串行、先 F2P、CC compact+thrash、默认 checklist；**不做** Plan mode / edit checkpoint / 子 Agent / Edit 主面 |
| Cursor？ | **采纳**「读结果再决策」；**保持不做**语义 Ret / Mode / Plan / Debug 插桩 / checkpoint / HITL / Rules 引擎 / 无限步 |
| Critic？ | **加**：默认 rules_first；不替代 Verifier / lint / path-safety |
| Memorizer？ | **本方案不纳入** |
| 运行进度？ | **采纳**：阶段进度摘要（CLI/日志/可选 JSONL）；与 Trace 同源；不做 TUI Dashboard |
| 先优化什么数字？ | nonempty → verified →（其后）官方 resolved |

---

## 13. 确认项（实现前）

Claude Code（§5.3）与 Cursor（§5.4）对比项已按裁决写入。实现前仍请确认：

1. SWE adapter 默认 `patcher_primary`，是否同意？
2. primary **彻底不调用** Loc/Ret（含弱锚），是否同意？
3. Phase A 是否捆绑「超时硬停」，还是先切流 + Critic rules + path-safety + 未读不可写？
4. Critic 默认 **`rules_first`**（推荐）还是 Phase A 就上 `llm`？
5. Phase B 是否将 **`apply_patch` 优先 + 磁盘真源 + 未读不可写 + edit-time lint** 作为 nonempty 硬门槛（推荐：是）？
6. 工具史 **compact（含 thrash 停）** 放 Phase B（推荐）还是 C？
7. `pipeline` 模式保留多久（仅对照 / 随后删除 Loc·Ret 代码）？
8. **进度输出**：Phase A 是否捆绑 CLI 最小事件集（推荐：是）？默认通道 stderr 还是 `progress.jsonl`？是否需要 SSE？

确认后按 Phase A → B → C 开分支（建议：`bonus/patcher-primary-swe`）。
