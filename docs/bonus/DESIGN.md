# FixLoop Bonus 设计说明

> 架构、边界、现状、面试要点与 Gap 说明。待实现条目见 [bonus.md](../bonus.md)。

## 目录

| 章 | 能力域 | 主要模块 |
|----|--------|----------|
| [1](#1-agent-运行时) | Agent 运行时 | `runtime.py` · `bootstrap.py` · `config.py` |
| [2](#2-agent-loop--react) | Agent Loop / ReAct | `agent_loop.py` · `callbacks.py` |
| [3](#3-context-工程) | Context 工程 | 八段投影 · 五 section 实现 · L0–L5 |
| [4](#4-分层记忆) | 分层记忆 | 四层 · 读写路径 · 召回 · 面试要点 |
| [5](#5-prompt) | Prompt | `prompt_prefix.py` · `src/prompts/` |
| [6](#6-agent-tool) | Agent Tool | `tools.py` · `schema_utils.py` · `src/tools/` |
| [7](#7-toolgateway) | ToolGateway | `middleware.py` · `tool_policy` |
| [8](#8-工具安全闸口) | 工具安全闸口 | `tool_executor.py` · `security.py` |
| [9](#9-硬上限与工具配额) | 硬上限 / 工具配额 | `QuotaEnforcer` · `AgentConfig` |
| [10](#10-限流--熔断--降级) | 限流 / 熔断 / 降级 | `circuit_breaker.py` · `clients.py` |
| [11](#11-checkpoint-断点恢复与续跑) | Checkpoint 续跑 | `checkpoint.py` · `task_state.py` |
| [12](#12-multi-agent-编排) | Multi-Agent 编排 | 角色 · 流水线 · **State** · Blackboard · 并发 · 生命周期 |
| [13](#13-skill) | Skill | `src/skills/*.yaml` |
| [14](#14-json-格式输出保证) | JSON 输出保证 | `output_parsers.py` |
| [15](#15-自愈闭环) | 自愈闭环 | `orchestrator.py` · `repair/verify.py` |
| [16](#16-docker-沙箱) | Docker 沙箱 | `harness/` · `sandbox_tools.py` |
| [17](#17-patch-与-verify) | Patch 与 Verify | `patch_applier.py` · `verify.py` |
| [18](#18-敏感信息处理) | 敏感信息处理 | `security.py` · `run_store.py` |
| [19](#19-链路可观测) | 链路可观测 | `run_store.py` · `replay.py` · `callbacks.py` |
| [20](#20-消融实验与评测) | 消融实验 / 评测 | `src/eval/` |
| [21](#21-cli--repl本地) | CLI · REPL（本地） | `agent_runtime/cli.py` · `src/cli.py` |
| [22](#22-配置--插件--可靠性) | 配置 / 插件 / 可靠性 | `fixloop.yaml` · entry_points |
| [23](#23-意图识别与路由) | 意图识别 / 路由 | `_parse_issue` · `_match_skill` · `_has_save_intent` |
| [24](#24-压测与容量) | 压测 / 容量（可选） | Locust/k6 · 沙箱池 |
| [25](#25-演示--文档--测试) | 演示 / 文档 / 测试 | Demo · ADR · pytest |
| [26](#26-输出质量--幻觉探针--judge-eval) | 输出质量 / 幻觉探针 | `src/eval/` · `patch_applier.py` |
| [27](#27-检索增强--embedding--查询改写) | 检索增强 | `retriever.py` · `memory/` |
| [28](#28-mcp--function-calling-适配) | MCP / FC 适配 | `tool_gateway.py` · `clients.py` |
| [29](#29-agent-范式落地) | Agent 范式 | `orchestrator.py` · `agent_loop.py` |
| [附录 A](OUT_OF_SCOPE.md) | Web 产品化 | 🚫 不实现 · 设计归档 |

---

## 1. Agent 运行时

> **设计边界**  
> 运行时 = **单 Agent 实例**的生命周期管理：session、tools registry、config、workspace 锚定、与 ModelClient 交互。  
> **不含** L2 修复编排；L2 通过 `repair_factory` 创建多个运行时实例。  
> **现状 ✅**：`Agent.ask()` · `execute_tool()` · session 持久化 · prefix hash · dry_run 透传。


---

## 2. Agent Loop / ReAct

> **设计边界**  
> ReAct = **Reasoning → Acting → Observation → Recording** 四阶段循环，直至 `<final>` 或 `max_steps`。  
> 双路径：**文本 XML/JSON 解析** + **原生 `tool_use` block**（provider 支持时）。  
> **现状 ✅**：单步循环 · 工具结果写 history · parse 失败指数退避 · `TaskState.tool_steps` 计数。  
> **Gap**：无显式 **Plan / TodoList** 阶段；ReAct 直接从 reasoning 进入 acting。

### 2.1 用户中断与取消

> **现状**：审批 `KeyboardInterrupt` → 拒绝工具（`_approve` 部分 ✅）；**无** `CancellationToken` · Loop 未统一处理 Ctrl+C · 工具执行中不可协作式 abort。

**收到 cancel 时分阶段处理**：

| 阶段 | 动作 | 状态 |
|------|------|------|
| 等待模型 | 关闭 stream / abort HTTP；不再发起新 completion | `stop_reason=user_cancel` |
| tool 调度前 | 检查 cancel flag；**不进入** Acting | 跳过 pending tools |
| **tool 执行中** | 按工具类型（见下表） | trace 记 `in_flight_tool` |
| run 收尾 | `_finalize_run`：checkpoint · SessionStore · trace `run_cancelled` | `TaskState.status=stopped` |

**工具执行中的 cancel 策略**（核心）：

| 工具 | cancel 时 | 理由 |
|------|-----------|------|
| `read_file` / `search` | 等待完成 **或** subprocess `terminate` | 无副作用；kill 仅丢本轮 observation |
| `write_file` / `patch_file` | **等当前调用返回** → `pre_tool snapshot` **回滚**；禁止 kill 中途 | 防半写 / torn patch |
| `run_shell` | **进程组 SIGTERM** → grace（如 3s）→ **SIGKILL** | 子进程必须收束 |
| L2 `sandbox_verify` | **`container.kill()`** + `restore_repo_snapshot` | 与 timeout 同路径（[§16](#16-docker-沙箱)） |

### 2.2 执行前 Plan · TodoList

> **设计边界**  
> 在 **ReAct 主循环开始前**（或 L2 repair 每 phase 开始前），用 **轻量 LLM 或规则** 生成结构化 **TodoList**，作为当轮 **state** 注入 Context（[§3.1 八段模型](#31-设计原则)），引导后续 tool 调度；**不是**替代 ReAct，而是可观测的计划层。

```
user_message / issue
        ↓
  (可选) Plan 阶段 — light_client / Planner
        ↓
  TodoList [{id, content, status: pending|in_progress|done|cancelled}]
        ↓ 写入 session["plan_todos"] · TaskState · trace plan_created
  ReAct loop — 每 step 可更新 todo 状态 · emit todo_updated
        ↓
  final / stop — 未 done 项进 report 摘要
```

| 层级 | 计划产物 | 模块 |
|------|----------|------|
| **L1 ask** | 3–7 条可执行 todo（读文件/搜索/改代码） | `agent_loop.py` · `TaskState` |
| **L2 repair** | `RepairPlan.subtasks` + phase todo | Orchestrator（[§12.8](#128-子问题拆分)） |
| **REPL** | `/todos` 展示 · 手动 mark done | [§21](#21-cli--repl本地) |

**与 RepairPlan 区别**：TodoList = **单 Agent 当轮执行清单**（细粒度、可变）；RepairPlan.subtasks = **L2 跨 Agent 子问题**（粗粒度、结构化）。

**Gap**：无 Plan phase · 无 todo schema · 无 trace 事件 · 空转检测（§2 backlog）未读 todo 进度。

### 2.3 Loop Engineering · Middleware · 防死循环

> **Loop Engineering** = 把 Agent 运行环 **可观测 · 可中断 · 可限幅** 的工程化；FixLoop 落点为 `AgentLoop` + `AgentCallback` + trace。

```
Context.build → pre_model hooks
    → Model (reasoning)
    → post_model → parse tool_calls
    → pre_tool → ToolGateway → ToolExecutor → post_tool
    → record history / memory / trace
    → loop_detect / stall_detect / cancel check
```

| 机制 | 模块 | 验收 |
|------|------|------|
| Middleware 链 | `callbacks.py` · `agent_loop.py` | native/XML 双路径同 invoke |
| 死循环检测 | `agent_loop.py` | 同 `(tool, args_hash)` K 次 → circuit_breaker |
| stall / 漂移 | `agent_loop.py` | 无 progress N 步 → stop_reason=stall |
| 职责边界 | Orchestrator + Gateway | route/skill/tool 集不由模型自改 |

**Gap**：Callback 未全覆盖 native 路径 · 无 args_hash 循环检测 · stall 与 todo 进度未联动。


---

## 3. Context 工程

> **一句话（面试）**：Context = **canonical 真相源 + prompt 投影**；目标 **八段模型**（system/task/state/knowledge/tools/skills/memory/history），现状 `build()` 用 **五 section** 实现并逐步迁移（[§3.2](#32-五-section-组装与-token-预算)）。  
> **模块**：`context_manager.py` · `prompt_prefix.py` · L2 Orchestrator 手工 prompt（目标统一 `TokenBudget`）。

### 3.1 设计原则

| 概念 | 含义 | 面试怎么说 |
|------|------|------------|
| **Context** | 为**当前任务**组装的全部信息 | 「当轮视图，会裁剪」 |
| **Prompt** | 一次 API 的 messages = **prompt_projection** | 「build() 产物，不是 session 全量」 |
| **canonical** | 会话真相源，压缩不修改原文 | 「history 只追加，丢信息可回滚重投影」 |
| **Memory** | 跨轮存储，经 section 注入 | 「存全量、读子集，见 §4」 |

**八段 Context 模型（目标） vs 现状五 section（实现）**

| 八段 | 含义 | 现状映射 | 目标 |
|------|------|----------|------|
| **system** | 角色 · workspace · 全局 rules | `prefix` 内 workspace/rules | 独立 section · cache 稳定 |
| **task** | 当前 user/issue 全文 | `request` + `## 当前任务` ✅ | 永不裁 · 钉扎 |
| **state** | phase · todo · step · 结构化进度 | **Gap** 未进 build | `plan_todos` · TaskState 投影 |
| **knowledge** | durable · precedent · 检索笔记 | `relevant` | knowledge section · RAG |
| **tools** | 本轮 tool 签名 | `prefix` 内 tools 段 | 独立 section · 仅启用集 |
| **skills** | 策略 · 示例 patch | L2 块；L0 off | 索引 + 按需全文（§13.4） |
| **memory** | Working：summary · recent_files | `memory` ✅ | 与 knowledge 分工 |
| **history** | 对话投影 | `history` + L0–L5 ✅ | canonical jsonl |

**Fill 顺序（目标）**：system → tools → skills → memory → knowledge → state → history → task  
**Fit 优先级（先裁）**：task > system > state > memory > knowledge > skills > tools > history

**三条纪律**

1. **Task 不可丢**：`task`/`request` 永不 `fit()`（现状 ✅）。  
2. **稳定段 cache**：`system`+`tools` 参与 `prompt_cache_key` ✅。  
3. **Skills/Knowledge 按需**：索引进窗、全文匹配后注入；海量 Skill 见 [§13.4](#134-海量-skill-加载)。

```
canonical                          build() 投影
────────────────────────────────────────────────
session["history"]      → history (+ L0–L5)
session["memory"]       → memory + relevant(knowledge)
session["plan_todos"]   → state (目标)
Agent._prefix           → system + tools (+ skills 索引)
_match_skill            → skills 全文 (按需)
user_message            → task ✅
                          → metadata.sections + cuts[]
```


### 3.2 五 Section 组装与 Token 预算

> **实现说明**：代码层仍用 **五 section 名**（`prefix` · `memory` · `relevant` · `history` · `request`）；[§3.1 八段模型](#31-设计原则) 为语义目标，迁移时拆 `prefix`→system/tools/skills、拆 `relevant`→knowledge、新增 `state`。

| Section | 预算常量 | 内容 | 裁剪 |
|---------|----------|------|------|
| `prefix` | 2000 | System · workspace · rules · tool 签名 | 超 TOTAL 时 fit |
| `memory` | 800 | task_summary · recent_files · file_summaries | 同上 |
| `relevant` | 600 | Episodic + Semantic + Durable top-k | 同上；检索见 §4.4 |
| `history` | 2600 | LLM 摘要 / 规则压缩 / 最近 6 条完整 | 同上 |
| `request` | **∞** | 当前 user_message | **永不裁** ✅ |

**总预算**：`TOTAL_BUDGET = 6000` · `TokenBudget` 用 tiktoken（未知模型 → cl100k_base）。

**填充顺序 vs 保护优先级**（面试常问）：

| 维度 | 顺序 | 含义 |
|------|------|------|
| 填充顺序 | prefix → memory → relevant → history → request | 前者先占 budget |
| 保护优先级 | request > prefix > memory > relevant > history | 后者先被 fit |

> **Gap（诚实讲）**：`BUDGET_*` 常量已定义，但 `add_section` 尚未对每 section 单独 enforce 硬顶——仅 TOTAL 双限；超 section 预算时仍可能挤占后续 section 空间。

**缩放公式**（现状 ✅，`scaled_section_budget`）：

```
section_limit = max(1, int(BUDGET_* × prompt_budget / REF_TOTAL_BUDGET))
history_window = scaled_section_budget(BUDGET_HISTORY, prompt_budget)  # L2–L5 触发基准
```

| `prompt_budget` | 典型场景 | history window（约） |
|-----------------|----------|----------------------|
| 6_000 | 单测 / 旧布局 | 2_600 |
| 100_000 | 默认 AgentConfig | ~43_333 |

**`add_section` 行为**（现状 ✅）：

1. 计数 section 文本 token  
2. 若 `used + section_tokens > total_limit` → 对**当前 section** 调用 `TokenBudget.fit()`  
3. 累加 `metadata.sections[name]` · `cuts[]`（目标：每 section 硬顶 + 总顶双 enforce）

**与 L0–L5 关系**：history section 的 **window** = `history_window_budget(total)`；L2–L5 阈值按 window 比例触发（见 [§3.3](#33-压缩管线-l0l5)）。压缩只改 history **投影**，不改 canonical。

**Gap（预算接线）**：

| 项 | 状态 |
|----|------|
| per-section 硬顶 | 常量有 · enforce 弱 |
| `tier_pins.yaml` | 声明有 · L2 Orchestrator 未读 |
| L2 手工 prompt | 未走 `TokenBudget.fit()` · 未写 cuts metadata |
| native tools 路径 | history 未走 L0–L5 全管线 |

**Prompt Cache**（现状 ✅）：`metadata["prompt_cache_key"] = prefix.hash` 透传 ModelClient。


### 3.3 压缩管线 L0–L5

> 参考 Claude Code / Cursor 分级口径；FixLoop **已实现完整 L0→L5 管线**（`compression_pipeline.py`），经 `ContextManager._get_compressed_history()` 统一调用；**canonical `session["history"]` 不动**，只改投影副本。  
> **模块**：`compression_pipeline.py` · `tier_policy.py` · `turn_tracking.py` · `context_manager.py`

| 级别 | 名称 | 触发 | 动作 | FixLoop |
|------|------|------|------|---------|
| **L0** | Tier Guard | 组装前 | 减不该进窗的内容 | ✅ `tier_policy.l0_filter_history`：allowed_tools · 丢弃 rejected/空/low-value · 钉扎 user/error/summary/current turn；Skills/MCP 按需 **待接** |
| **L1** | Budget Reduction | 始终 | tool 返回硬截断 | ✅ **token 级** `TOOL_TRUNCATION_TOKENS` + 重要行优先 |
| **L2** | Snip | **55%** history window | 整轮删除低价值只读探索轮 | ✅ `l2_snip` · snip 标记 · 保护 recent 2 turn + 尾部保护区 |
| **L3** | Microcompact | **70%** window | 旧 tool → `[ref:#id]` stub | ✅ `l3_microcompact` · metadata 侧表 `l3_refs` |
| **L4** | Collapse | **82%** window | 旧 turn 折叠为摘要行 | ✅ `l4_collapse` · canonical 不动 |
| **L5** | Auto Compact | **100%** window | LLM 摘要前半段 | ✅ `l5_auto_compact` · 在 L1–L4 **之后**触发 · `_summary_cache` 内存 · 失败保留最近 8 条 |

**触发口径**（相对 **history section 预算**，随 `AgentConfig.prompt_budget` 等比缩放；参考布局 6000 总预算时 history window = 2600）：

| 预算示例 | history window | L2 55% | L3 70% | L4 82% | L5 100% |
|----------|----------------|--------|--------|--------|---------|
| 6k（测试） | 2600 | 1430 | 1820 | 2132 | 2600 |
| 100k（默认） | ~43333 | ~23833 | ~30333 | ~35533 | ~43333 |

**L1 截断表**（**token 级**，现状 ✅）：

| 工具 | 上限 (tok) | 策略 |
|------|------------|------|
| read_file | 570 | Error/Fail/路径行优先，再填其余 |
| search | 230 | 同上 |
| run_shell | 145 | 同上 |
| list_files | 60 | 同上 |
| write_file / patch_file | 90 | 同上 |
| 默认 | 150 | `truncate_tool_content` |

**History 压缩路径**（`build()` → `run_compression_pipeline()`，现状 ✅）：

```
session["history"]（canonical）
  → L0 Tier Guard（TierPolicy：allowed_tools · turn_id 钉扎 · 过滤噪声）
  → L1 每条 tool token 截断
  → L2 Snip（>55% window）
  → L3 Microcompact（>70%）
  → L4 Collapse（>82%）
  → L5 Auto Compact（>100% window；LLM 摘要 old_half，失败 → 最近 8 条）
  → 格式化进 history section
       ├─ L5 已触发 → 直接渲染投影结果
       └─ 未触发 L5 → 最近 KEEP_RECENT_HISTORY=6 条完整
                      + 若 L2–L4 均未触发 → 旧段 `_compress_old_entries` 规则合并（read_file 路径等）
```

**保护区（L2–L4 豁免）**（现状 ✅）：

| 机制 | 说明 |
|------|------|
| **current turn_id** | `turn_tracking` 标记；当前 turn 内 user/assistant/tool 不 snip/compact/collapse |
| **最近 2 turn** | `L2_PROTECT_RECENT_TURNS` 硬保护 |
| **尾部 20k token** | `AgentConfig.tail_protect_tokens`（默认 20_000）；**跨边界整 turn 保护**；小 window 时 `effective = min(20k, window×2000/2600)` |
| **Error/Traceback** | `PROTECTED_KEYWORDS` · L3 跳过含 error 的 tool 正文 |

**L5 目标 schema**（部分 ✅）：用户任务 · 涉及文件 · 决策 · 未完成 · 下一步（当前 prompt 为 1–2 句英文摘要 + 300 字 cap）。

**Gap（诚实讲）**：

| 项 | 状态 |
|----|------|
| `chat_with_native_tools` 路径 | 仅 tool 返回走 **L1**（`truncate_tool_result_for_agent`），**未**经 L0–L5 全管线 |
| L2 Orchestrator 手工 prompt | 未复用 `run_compression_pipeline` · `tier_pins.yaml` 仅声明未接线 |
| L0 Skills/MCP | `skill_mode` 字段存在，默认 `off` |
| 阈值外置 yaml | 55/70/82/100% 仍硬编码于 `compression_pipeline.py` |


### 3.4 L2 Repair 与 Memory 衔接

> L1 用 `ContextManager.build()`；L2 Orchestrator **手工拼 prompt**，须复用同一预算纪律与钉扎区。

| 路径 | 组装方式 | Memory |
|------|----------|--------|
| L1 Agent | `build()` 五 section | `memory` + `relevant`（§4.4；**检索 query ≠ user 全文**） |
| L2 Localizer/Patcher | Orchestrator prompt 模板 | issue/stack **钉扎**；suspects/tests `fit()` |

### 3.5 上下文传递 · 压缩触发 · user 保护

| 规则 | 实现目标 |
|------|----------|
| **user / issue 永不压缩** | L0–L5 与 `fit()` 单测保护 current turn |
| **压缩触发可观测** | trace `compression_triggered{level,ratio}` |
| **L5 fact-pin** | issue/stack/suspect 进 tier_pins；摘要不得覆盖 |
| **temperature preset** | localizer/patcher/verifier 分配置 |

**Gap**：user 保护缺 enforce 单测 · 压缩触发未全进 trace · L5 与钉扎区未硬耦合。


---

## 4. 分层记忆

> **一句话（面试）**：FixLoop 用 **Working / Episodic / Durable / Semantic 四层** 分工「近因 · 工具笔记 · 跨会话规范 · 向量召回」；**写入在 invoke 后同步 hook 落盘**，**读取在 Context `build()` 按需投影**——记忆是 hint，**源码与 pytest 才是真相**。  
> **模块**：`agent_runtime/features/memory/` · `ContextManager._get_memory` / `_get_relevant` · `runtime.update_memory_after_tool` · `promote_durable_memory`。

### 4.1 设计原则

| 边界 | 含义 | 面试怎么说 |
|------|------|------------|
| **Memory ≠ Context** | Memory 跨轮存储；Context 是当轮裁剪视图（[§3](#3-context-工程)） | 「存全量、读子集；投影可丢，存储不丢」 |
| **Memory ≠ Ground Truth** | 读文件 / AST / stack / verify 才是权威 | 「similar_fixes 只 hint，不覆盖 Localizer 证据链」 |
| **按需读** | 启动不灌全量；`build()` 时检索 top-k | 对齐 Claude Code「按需 Read」，我们用 section 注入 |
| **同步写** | tool 后 / `ask_end` 一次落盘，本轮可读 | 对比偏异步的产品：repair 多 Agent 需要确定性 |
| **固定 topic** | Durable 仅 4 类 + `PREFIX_MAP`，LLM 不能自由建库 | 防幻觉污染、可审计、可 eval |

**Context 两路注入**（现状 ✅）：

| Section | 来源层 | 预算 | 内容 |
|---------|--------|------|------|
| `memory` | Working | ~800 | task_summary · recent_files · file_summaries |
| `relevant` | Episodic + Semantic + Durable | ~600 | 混合检索 top-k（需 query，否则空） |

### 4.2 四层模型与数据流

| 层 | 时间尺度 | 存储位置 | 检索方式 | 上限（现状） |
|----|----------|----------|----------|--------------|
| **Working** | 当前会话 | `session["memory"]` | 全量进 `memory` section | recent_files **8** · summaries **6** |
| **Episodic** | 当前会话 | `episodic_notes[]` | 关键词 + tag 打分 + 近 1h 加权 | **12** 条 |
| **Durable** | 跨会话 | `.agent/memory/topics/*.md` | 子串匹配 `retrieval()` | 4 固定 topic |
| **Semantic** | 会话内索引 | 内存 embedding 缓存 | cosine **>0.3**，与 Episodic 合并 | 最近 **20** 条 encode |

**Durable 固定 topic**（`DURABLE_TOPICS`）：`project-conventions` · `key-decisions` · `dependency-facts` · `user-preferences` — 经 `Convention:` / `Decision:` / `Dependency:` / `Preference:` 前缀路由。

```
写入（同步）                              读取（按需）
────────────────                          ────────────────
read/write/search/shell                   ContextManager.build()
    → update_memory_after_tool()              ├─ memory  ← Working 全量
    → Working + Episodic                      └─ relevant ← 混合检索 + Durable
ask_end + SAVE_INTENT
    → promote_durable_memory()              repair（目标）
    → topic.md upsert                         └─ similar_fixes ← Durable/Semantic
repair fixed（目标）
    → promote repair-precedent topic
```


### 4.3 写入管线

> **三条写入路径**，均在 **invoke 成功后**执行（非异步 fire-and-forget）。

| 触发 | 入口 | 写入层 | 现状 |
|------|------|--------|------|
| 每次 tool 成功 | `update_memory_after_tool` | Working + Episodic | ✅ read/write/search/shell |
| 每次 ask 结束 | `promote_durable_memory` | Durable | ✅ SAVE_INTENT + 前缀抽取 |
| repair 成功 | Orchestrator hook（目标） | Durable `repair-precedent` | 字段 `similar_fixes` 已预留 |

**Durable 写入七步**（目标完整版；步骤 1–2/5–6 部分 ✅）：

| # | 动作 | FixLoop |
|---|------|---------|
| 1 | 会话近因 | ✅ Working · task_summary |
| 2 | 抽取 candidates | 部分 ✅ 规则 `SAVE_INTENT_WORDS` · `_extract_promotions` |
| 3 | 路由 topic | ✅ `PREFIX_MAP` → 4 topic |
| 4 | 冲突检测 | 部分 ✅ `_upsert_entry` 按首行 subject；目标：权威序状态机 |
| 5 | upsert topic.md | ✅ `---` 分隔条目 |
| 6 | 更新 MEMORY.md 索引 | ✅ `_update_index()` |
| 7 | 健康 / Dream | 目标 |

**Candidate schema**（目标）：`{ key, value, source, confidence, evidence }` · `source`: user_explicit > user_implicit > assistant_inferred > system


### 4.4 召回与 Context 投影

> **混合检索**（现状 ✅ `retrieval_candidates_semantic`）：Episodic **关键词**（tag×3 + text×1 + 近 1h 加权）∪ **Semantic**（cosine>0.3）→ 按 `note_index` 去重 → top-k；再并 **Durable** 子串匹配。

| 策略 | 实现 | 面试要点 |
|------|------|----------|
| 无 query | `relevant` 留空 | 目标：降级用 `task_summary` |
| Semantic 不可用 | 仅 keyword，stderr 提示 | ✅ 不阻塞 `build()` |
| 超 budget | section 内截断 | 目标：task > files > summaries > episodic |
| hint 纪律 | 记忆不进钉扎区 | stack/issue 永不来自 memory |
| **用户输入过长** | 全文进 Context `request` ✅；**embedding 只用短 query** | 见下表；现状 gap：`_get_relevant` 直传全文 |

**长文本 vs Embedding 窗口**（`all-MiniLM-L6-v2` 约 **256 token**；现状 `encode()` **无截断**）：

> **原则**：LLM 需要完整 user/issue（[§3](#3-context-工程) `request` 钉扎）；**Semantic 检索只用派生 query**，二者分离。

| 场景 | 送入 Embedding | 做法 |
|------|----------------|------|
| L1 普通 ask | **不用**全文 user_message | 用 `task_summary`（≤300 字 ✅）或 tiktoken 截断 query |
| L2 长 issue / stack | **不用**全文 issue | 规则抽：`Exception` 类型 + 末 3 帧 stack + `suspect_files` |
| Episodic 笔记入库 | 已限 **300 字** ✅ | 入库前 truncate；一般不需 chunk |
| Durable / repair precedent | 单条 ≤500 字（闸口 ✅） | 超长 topic **分块 embed**，检索取 chunk max sim |
| Query 仍超窗 | `EMBED_MAX_TOKENS` | **head+tail** 截断（stack/issue 保留 **Error 段 + 末帧**） |

```
user_message（任意长）
    ├─→ Context request section     全文，永不裁（LLM）
    └─→ derive_embed_query()
            ├─ task_summary（优先）
            ├─ 规则抽取（issue_type · stack tail · paths）
            └─ tiktoken fit(EMBED_MAX_TOKENS, strategy=head_tail)
                └─ SemanticMemory.search(query_short)
                    ∪ keyword 全量匹配（不受 256 限）
```


### 4.5 质量 · 衰减 · 隔离

**冲突处理**（Durable 目标完整版）：

| 类型 | 条件 | 处理 |
|------|------|------|
| None | 新 key | 写入 |
| Equivalent | 同 key 同 value | 更新 last_seen |
| Override | 新 candidate 权威更高 | 覆盖 |
| Invalid | schema / 闸口失败 | 拒绝 + trace |

**衰减与 GC**：file_summary 随 patch invalidate · episodic 超 12 条 FIFO · durable LRU 归档 · **Memory Dream** 后台去重（不阻塞交互）。

| 隔离层 | 标识 | 目标路径 |
|--------|------|----------|
| 项目 | `project_id` | `.agent/memory/`（现状）→ `data/memory/projects/{id}/` |
| 会话 | `session_id` | `session.json` + working/episodic |

> **单用户本地**：无 `user_id` / 多租户路径；若 fork 为 SaaS 见 [OUT_OF_SCOPE.md](OUT_OF_SCOPE.md)。


### 4.6 L2 Repair 记忆桥接

> **闭环**：读 precedent → 辅助 Patcher；写 precedent → 反哺 L1 Durable。`RetrievedContext.similar_fixes` 字段 ✅，读写管道为目标 P1。


### 4.7 运维与面试要点

**REPL**


**30 秒电梯陈述**

> 我们把记忆分成四层：Working 管当前任务和最近文件，Episodic 记工具笔记，Durable 用固定 topic 的 Markdown 跨会话存规范和修复先例，Semantic 给 Episodic 做 embedding 召回。写入在 tool 和 ask 结束时同步落盘，读取只在拼 prompt 时按需检索，并且明确记忆不能替代读文件和测试。L2 修复流水线会通过 similar_fixes 读历史先例、成功后再写回。

**高频面试 Q&A**

| 问题 | FixLoop 答案 |
|------|--------------|
| 为什么不用向量库 / MySQL？ | MVP **文件优先**（md + session json + embed_cache），零外部依赖、可 git diff、易 eval |
| 怎么防记忆污染？ | 固定 topic + 写入闸口 + SAVE_INTENT + 权威序冲突 + 不信任覆盖 ground truth |
| 长短记忆怎么协同？ | Working 全量近因 → Episodic 工具笔记 → Durable 晋升；Context 按 budget 投影 |
| 和 Claude Code 差异？ | 他们偏 LLM 自判 + 异步；我们 **规则+前缀路由 + 同步 hook**，Multi-Agent repair 要确定性 |
| 和 Cursor Memories 差异？ | 他们用产品级 RAG；我们 **四层显式模型 + L2 precedent 闭环**，可开源可测 |
| Semantic 挂了怎么办？ | keyword 降级 + durable 子串仍可用，`build()` 不 fail |
| 用户输入很长，embedding 窗口不够？ | **LLM 看全文、向量看短 query**；task_summary / 规则抽取 / head+tail 截断；keyword 不受 256 限；语料侧 episodic≤300✅ |

**业界简表**

| 产品 | 组织 | 写入 | 召回 | FixLoop 差异 |
|------|------|------|------|--------------|
| Claude Code | 项目 md + 自动记忆 | LLM 自判，偏异步 | 按需 Read | 我们同步 hook + 固定 topic |
| Cursor | Rules + Memories RAG | 用户 + 推断 | 索引注入 | 我们四层分工 + repair precedent |
| FixLoop | 4 层 + topic md | **tool/ask 同步** | Context section + similar_fixes | 可测、可隔离、L2 闭环 |

### 4.8 用户画像 · 遗忘 · Embedding 迁移

| 能力 | 存储 | 模块 |
|------|------|------|
| **用户画像** | durable `preferences` topic · structured kv | `durable.py` pydantic |
| **遗忘/衰减** | episodic GC · confidence decay · topic TTL | §4.5 backlog |
| **Embedding 迁移** | rebuild index · `.agent/embed_cache/` | `memory/` embed 层 |
| **查询改写** | HyDE / 多 query → `derive_embed_query()` | §4.4 · §27.3 |
| **知识卡片** | durable fact：`topic·fact·source·confidence` | §27.4 |

**Gap**：preferences 无 structured schema · embed 切换无 rebuild 流程 · HyDE 未实现。


---

## 5. Prompt

> **设计边界**  
> **Prompt** = 一次 API 调用的 messages 投影；分 **L1 稳定 prefix**（利于 cache）与 **L2 角色 system**（按 Agent 外置）。  
> **模块**：`agent_runtime/prompt_prefix.py` · `src/prompts/*.txt` · Orchestrator `_localizer_prompt` 等手工模板。  
> **现状 ✅**：`prompt_prefix.hash` · dry_run/approval rules · 四角色 prompt 文件 · issue/stack 钉扎（Orchestrator 侧）。

### 5.1 Prefix · Cache · Rules

| 组件 | 八段 | 内容 | Cache |
|------|------|------|-------|
| workspace | **system** | cwd · git · doc 摘要 | fingerprint 变则失效 |
| rules | **system** | dry_run · approval · 安全纪律 | 稳定 |
| tool signatures | **tools** | 本轮启用 schema | 随 `_tool_names` 变 |
| skill 索引 | **skills** | 仅 catalog 摘要（§13.4） | 命中后全文另段 |
| `prompt_cache_key` | system+tools | prefix SHA | ✅ ModelClient |

**外置规则**（目标）：`.agent/rules.md` · `.agent/examples.md` — few-shot 与项目约定不进代码。

### 5.2 L2 角色 Prompt

| Agent | 文件 | 变体维度 |
|-------|------|----------|
| Localizer | `localizer.txt` | stack 优先 · import 路径 |
| Retriever | `retriever.txt` | 广搜 vs `--fast-retrieve` |
| Patcher | `patcher.txt` | **issue_type 后缀**（type/import/logic/config） |
| Verifier | `verifier.txt` | Docker vs pytest |

> **与 §23 分工**：§23.3 决定**选哪套**变体；§5 维护**文案**与模板文件。

### 5.3 模板 · 调试 · 指标

- **User Message 模板化**（目标）：Jinja 渲染任务块，与 system prefix 分离（§3.2）。  
- **Skill 块注入**：`[Skill 提示]` = suggested_tools + example_patch（[§13.3](#133-注入与-eval)）。  
- **调试**：REPL `/prompt` 导出当前 projection；trace 记录 `sections` / `cuts`（§3.2）。  
- **Cache 指标**：`cache_read_tokens` / `cache_creation_tokens` → `report.json` · REPL `/session`。


---

## 6. Agent Tool

> **设计边界**  
> Tool = **dataclass 参数 schema + 执行函数 + 注册 metadata**；schema 唯一真相源 = `auto_schema` / `auto_validate`。  
> L1 通用工具在 `agent_runtime/tools.py`；L2 领域工具在 `src/tools/` + `registry.py`。  
> **现状 ✅**：read/search/write/patch/shell · sandbox_verify · ast_parse · stack_parse · git_* · find_test。

### 6.1 L1 通用工具

| 工具 | 职责 | 安全/配额 |
|------|------|-----------|
| `read_file` | 读 workspace 内文件 | Gate validate · duplicate |
| `grep`（目标） | **rg 封装**内容搜索 · `glob`/`max_results` | 不走 shell · quota · 见 [§6.5](#65-grep-工具rg-封装) |
| `search` | 兼容层；目标转调 `grep` | 结果截断 · quota |
| `write_file` | 全文写 | 审批 · snapshot · 原子写（目标） |
| `patch_file` | unified diff apply | 同上 · 多 hunk |
| `list_files` | 目录列举 | depth/glob（目标） |
| `run_shell` | 子进程 | env 白名单 · 超时 · quota |

### 6.2 注册与 L2 领域工具

| 工具 | Agent | 说明 |
|------|-------|------|
| `stack_parse` | Localizer | traceback → 帧列表 |
| `ast_parse` | Localizer | AST · 注释剥离（§18） |
| `git_blame` / `git_diff` | Localizer/Retriever/Patcher | 历史上下文 |
| `find_test` | Retriever/Patcher | 相关测试 |
| `sandbox_verify` | Verifier | Docker/pytest（§16） |

**注册路径**：L1 `Agent.register_tool` · L2 `src/tools/registry.py` + factory 按 Agent 挂载。

**ToolGroup**（目标）：`inspect_file` = read + ast 原子组合，占 1 quota。

### 6.3 Schema 工程 · Manifest

| 能力 | 模块 | 说明 |
|------|------|------|
| `auto_schema` | `schema_utils.py` | dataclass → JSON schema |
| `auto_validate` | 同上 | 参数校验唯一入口 |
| **tools.yaml**（目标） | `.agent/tools.yaml` | 按 Agent 可见性 merge manifest |
| 与 Gateway 对齐 | `REPAIR_PERMISSION_TABLE` | schema 声明的工具 ⊆ 权限表 |

**纪律**：新增 L2 工具 = dataclass + registry 条目 + Gateway 行 + 单测。

### 6.4 工具分发 · 参数校验 · Retriever 路径

| 设计点 | 说明 |
|--------|------|
| **参数校验** | `auto_validate` 失败 → trace + 可选 repair prompt |
| **Retriever 路径** | stack→read→**grep** 规则优先；`--fast-retrieve` 跳过 LLM |
| **子 Agent 可见性** | `.agent/tools.yaml` per-role merge |

### 6.5 Grep 工具（rg 封装）

> **目标**：一等工具名 **`grep`**（对齐 Claude Code / 面试口径），替代模型通过 `run_shell rg` 或语义模糊的 `search` 搜代码。  
> **实现**：`GrepArgs` dataclass → `tool_grep()` → 复用/抽取 `_search_rg` · `IGNORED_PATH_NAMES` · `ToolContext.resolve`。  
> **`search` 策略**：短期并存；`tool_search` 委托 `tool_grep`；prompt 与 schema description 引导新调用用 `grep`。

| 参数 | 说明 |
|------|------|
| `pattern` | 正则或字面量（rg 默认 smart-case） |
| `path` | workspace 内根路径，默认 `.` |
| `glob` | 传给 `rg --glob`，如 `*.py` |
| `ignore_case` | `-i` |
| `context_lines` | `-C` |
| `max_results` | 截断 + `...N more` |

| Agent | grep |
|-------|------|
| Localizer / Retriever | ✅ |
| Patcher / Verifier | ✗（经 ToolGateway） |

**验收**：`tests/test_tools.py` · Gateway 矩阵 · Retriever 规则路径 trace `retrieval_path=rule`。


---

## 7. ToolGateway

> **设计边界**  
> ToolGateway = Agent 与 ToolExecutor 之间的**权限中间件**；Agent 只见普通 tool error，**不知**被 Gateway 拦截。  
> **模块**：`src/middleware.py` · `agents/factory.py`（`tool_policy=gw.can_call`）。  
> **现状 ✅**：`REPAIR_PERMISSION_TABLE` · `dispatch()` · `permission_denied` metadata。

### 7.1 权限表

```python
# 核心规则（现状 ✅）
write_file / patch_file  → patcher only
sandbox_verify           → verifier only
ast_parse / stack_parse  → localizer only
read/search/list         → *（所有 Agent）
```

| 设计点 | 说明 |
|--------|------|
| 默认拒绝 | 表外 tool → `can_call` False |
| `*` 通配 | 读类工具共享 |
| 动态 grant/revoke | API 有 · eval 未用 |

**Gap**：权限表硬编码 Python dict；目标 **yaml 外置** + 与 `.agent/tools.yaml` 校验一致。

### 7.2 调度 · 审计

| API | 行为 |
|-----|------|
| `can_call(agent, tool)` | 布尔；供 AgentLoop policy hook |
| `dispatch(agent, tool, fn)` | 拒绝 → `ToolExecutionResult(permission_denied)` |
| `grant` / `revoke` | 运行时扩展（目标进 trace） |

**审计**（目标）：`permission_denied` 计数 → trace · `agent_errors` · report（与 §19.3 交叉）。

### 7.3 Gateway vs ToolExecutor

| 层 | 拒绝原因 | `tool_error_code` |
|----|----------|-------------------|
| **ToolGateway** | 角色不允许 | `permission_denied` |
| **ToolExecutor** | 参数/路径/配额/闸口 | `quota_exceeded` · `validation_error` · gate id |

面试：**纵深** = Gateway（角色）+ Executor 九道闸（[§8](#8-工具安全闸口)）+ Orchestrator（阶段写窗口 §12.6）。

### 7.4 Function Calling 执行环

```
Model tool_calls / XML <invoke>
    ↓ parse
ToolGateway.can_call(agent, tool)
    ↓ dispatch
ToolExecutor (Gates 1–9)
    ↓
ToolExecutionResult → history / trace
```

| 原则 | 说明 |
|------|------|
| **模型只见 schema** | prompt 注入 tool 签名；不暴露 Python 函数指针 |
| **执行仅经 Gateway** | AgentLoop 禁止 bypass |
| **native ≡ XML trace** | 同一 `tool_executed` schema |

**与 MCP**：MCP = 进程间工具协议（可选 P3 shim）；FixLoop 主线 = 本地 Gateway + dataclass schema（§28）。


---

## 8. 工具安全闸口

> **设计边界**  
> 九道闸口顺序执行，**任一失败返回 `ToolExecutionResult`，不抛异常**。  
> **现状 ✅**：白名单 → 存在 → validate → quota → duplicate → dry_run → approval → pre snapshot → exec → post snapshot。


---

## 9. 硬上限与工具配额

> **设计边界**  
> **硬上限** = 不可协商的计数上限，达限即 `quota_exceeded`。  
> **现状 ✅**：writes ≤20 · shell ≤10 · total ≤50（单 session）；`max_steps` · `max_retries` · sandbox mem/cpu。


---

## 10. 限流 · 熔断 · 降级

> **设计边界**  
> **限流** = 请求速率控制；**熔断** = 连续失败断开；**降级** = 备用路径降能力保可用。  
> **现状 ✅**：`CircuitBreaker`（5 失败 / 30s 恢复）· rg→Python grep · Docker→host pytest · LLM 摘要失败→规则 trim。

### 10.1 模型 API


### 10.2 修复流水线降级



---

## 11. Checkpoint 断点恢复与续跑

> **设计边界**  
> L1 checkpoint = 跨 **ask/session** 恢复；L2 repair checkpoint = 跨 **repair 阶段** 恢复。  
> **现状 ✅**：`create_checkpoint` · `evaluate_resume_state` · key_files freshness hash。


---

## 12. Multi-Agent 编排

> **设计边界**  
> **真 Multi-Agent** = 不同 Tool 集合 + 不同 system prompt + **独立运行时实例**。  
> **State** = 结构化 dataclass 在 Agent 间流转（`src/state.py`）；**不靠自然语言协议**。  
> **通信** = 主路径 **`RepairState` 字段** + 辅助 **Blackboard** KV（已实现，目标接入 Orchestrator）。  
> **Orchestrator** = 纯 Python 调度（不调 LLM）：持有 State · 阶段编排 · 冲突仲裁 · 终止 · 恢复。  
> **现状 ✅**：Localizer∥Retriever → Patcher → Verifier · `RepairState` · `ToolGateway` · `max_retries=3` · ThreadPool · `repo_snapshot`。  
> **PR #83**：`tool_policy` · `VerifyStrategy` · `RepoPatchApplier` · `RepairPipelineMixin` · `output_parsers` · baseline factory。

### 12.1 角色与机制

| Agent | 职责 | Tool（经 ToolGateway） | 产出 |
|-------|------|------------------------|------|
| **Localizer** | 堆栈 + AST 定位 | read · search · stack/ast；**不可 write** | `SuspectLocation[]` |
| **Retriever** | 代码/测试/Git 上下文 | read · search · git_*；**不可 patch** | `RetrievedContext` |
| **Patcher** | 生成并应用补丁 | read · write · patch；**不可 sandbox** | `CandidatePatch[]` |
| **Verifier** | 隔离验证 | sandbox_verify；**不可改代码** | `VerificationResult` |

> 权限在 **ToolGateway** 强制（[§7](#7-toolgateway)）；Orchestrator 只消费结构化 JSON。


### 12.2 流水线编排

> 入口 **意图识别** 见 [§23](#23-意图识别与路由)（`_parse_issue` + `_match_skill` → `RepairPlan`）。

```
parse_issue + match_skill
        ↓
  Localizer ∥ Retriever    ← 只读，并行
        ↓ merge
     Patcher                 ← 唯一写阶段，串行
        ↓
     Verifier
        ↓
  fail? → feedback → Patcher（≤ max_retries）
```


### 12.3 State 三层模型

> **核心原则**：Agent 间只交换 **typed dataclass / JSON**，Orchestrator 是唯一「写流程 State」的组件；各 Agent 的 L1 运行时状态**不共享指针**。

```
┌─────────────────────────────────────────────────────────────┐
│  L2 流程层   RepairState          Orchestrator 持有，跨 Agent │
│              repair_plan · suspects · context · patches ·   │
│              verification · feedback · retry · status       │
├─────────────────────────────────────────────────────────────┤
│  L2 交换层   Blackboard (KV)      可选；同 key 冲突检测      │
│              source_agent · TTL · read_related(prefix)      │
├─────────────────────────────────────────────────────────────┤
│  L1 运行层   TaskState            单次 ask()；每 Agent 独立  │
│              tool_steps · stop_reason · checkpoint_id       │
│              + session["history"] / memory（私有，不跨 Agent）│
└─────────────────────────────────────────────────────────────┘
         Orchestrator 读/写 RepairState
              ↓ 拼 prompt          ↑ 解析 JSON 产物
         Localizer / Retriever / Patcher / Verifier（各持独立 L1 session）
```

| 层 | 模块 | 生命周期 | 谁写 | 谁读 |
|----|------|----------|------|------|
| **L2 流程** | `RepairState` | 单次 repair | Orchestrator 各 phase | Orchestrator · CLI · eval |
| **L2 交换** | `Blackboard` | repair 内 | Agent 经 Orchestrator（目标） | Orchestrator · prompt 构建 |
| **L1 运行** | `TaskState` | 单次 `ask()` | `AgentLoop` | checkpoint · trace · report |
| **L1 私有** | `session` / memory | 单 Agent ask | 该 Agent 的 runtime | 仅该 Agent `build()` |


### 12.4 RepairState 状态机与产物

> **`RepairState`**（`src/state.py`）= 单次 repair 的**唯一流程真相源**；各 Agent 产出写入对应字段，Orchestrator 驱动重试环。

**字段 ↔ Agent 映射**：

| 字段 | 类型 | 产出 Agent | 用途 |
|------|------|------------|------|
| `issue_input` | str | 用户/CLI | 原始 issue，prompt 钉扎区 |
| `repair_plan` | `RepairPlan` | Orchestrator `_parse_issue` | issue_type · suspect_files · skill |
| `suspect_locations` | `SuspectLocation[]` | Localizer | 定位 + confidence · reason |
| `retrieved_context` | `RetrievedContext` | Retriever | snippets · tests · similar_fixes |
| `candidate_patches` | `CandidatePatch[]` | Patcher | diff · original/patched_lines |
| `verification_result` | `VerificationResult` | Verifier | all_passed · failure_logs |
| `feedback` | str | Orchestrator | verify 失败 → 下一轮 Patcher |
| `retry_count` / `max_retries` | int | Orchestrator | 重试环控制 |
| `status` | str | Orchestrator | 终态（见下表） |
| `node_timings` / `agent_errors` | dict | Orchestrator | 可观测 · 降级依据 |

**`status` 状态机**（现状 ✅，`repair/pipeline.py`）：

```
pending
   ↓ parse_issue + localize∥retrieve
   ↓ patch → verify 循环
fixed          verify 全绿
patched        未启用 verify 且有补丁
exhausted      retry_count ≥ max_retries
failed         超时 / 无补丁 / 不可恢复错误
```

| 产物 dataclass | 关键字段 | schema |
|----------------|----------|--------|
| `SuspectLocation` | file_path · start/end_line · confidence · reason | ✅ `to_dict` / `from_dict` |
| `RepairPlan` | issue_type · suspect_files · reasoning | ✅ |
| `RetrievedContext` | related_tests · similar_fixes · snippets | ✅ |
| `CandidatePatch` | diff · original_lines · patched_lines | ✅ |
| `VerificationResult` | all_passed · failure_logs · lint_issues | ✅ |


### 12.5 Blackboard 与 Agent 通信

> **Blackboard**（`src/blackboard.py` ✅）= repair 内的 **KV 交换板**，补 `RepairState` 固定 schema 装不下的中间结论。  
> **纪律**：同 key **同 source 可覆盖**；**异 source 拒绝覆盖**并记入 `_conflicts`，由 Orchestrator 仲裁。  
> **Gap（设计债）**：模块 + 单测 ✅；**`orchestrator.py` 主路径未 import Blackboard**，并行 phase 仍直写 `RepairState` 字段。

| API | 行为 |
|-----|------|
| `write(key, value, source_agent, ttl?)` | 成功 True；冲突 False |
| `read(key)` | 单条；TTL 过期自动删 |
| `read_related(prefix)` | 前缀批量读（如 `suspect/`） |
| `snapshot()` | entries + conflicts 副本 |
| `resolve_conflict(key, winner_source)` | 手动仲裁 |

**Key 命名空间**（目标约定）：

| 前缀 | 示例 | 写入者 |
|------|------|--------|
| `suspect:` | `suspect:calc.py:42` | Localizer · Retriever |
| `context:` | `context:related_tests` | Retriever |
| `subtask:` | `subtask:A:status` | Planner / Orchestrator |
| `scratch:` | TTL 短暂存 | 任意 Agent |

**与 RepairState 分工**：

| 放 RepairState | 放 Blackboard |
|----------------|---------------|
| 各 Agent **最终产物**（suspects · patches · verify） | 中间假设、多源候选、子任务命名空间 |
| CLI/eval 需要的**稳定 schema** | TTL 短的 scratch · 并行 phase 暂存 |
| checkpoint / resume 必需字段 | 冲突待仲裁条目 |

**目标接线**（Orchestrator）：

```
Localizer/Retriever phase → write suspect:* / context:*
        ↓ merge + resolve_conflict
Patcher prompt ← read_related("suspect/") + RepairState 字段
        ↓
verify 失败 → scratch:feedback TTL · 仍落 RepairState.feedback
```

**与 §23**：`issue_type` 路由决定哪些 key 必填；composite 子任务用 `subtask:` 命名空间（§12.8）。


### 12.6 并发与一致性

> **读可并行、写必串行、验证在快照之后**。

| 场景 | 策略 | FixLoop |
|------|------|---------|
| Localizer ∥ Retriever read | 只读；无写者 | ✅ ThreadPool(2) |
| 多 Agent 同文件 write | **禁止**；仅 Patcher 可写 | ✅ ToolGateway |
| verify 前污染 | **repo_snapshot** + restore | ✅ 已有 |
| 同 repo 多 repair | 写锁 / 独立 temp workspace | **本地**文件锁 + temp workspace（[§21](#21-cli--repl本地) repair CLI） |
| 重复 read | Gate 5 duplicate | [§8](#8-工具安全闸口) |


### 12.7 冲突 · 终止 · 恢复

**用户 cancel 级联**（L2 Multi-Agent）：

| 层级 | cancel 入口 | 传播 | 工具执行中 |
|------|-------------|------|------------|
| **CLI** | **Ctrl+C** · REPL **`/cancel`** | Orchestrator `cancel_event.set()` | 见 [§2.1](#21-用户中断与取消) |
| Orchestrator | 阶段边界检查 | 不再调度下一阶段；**中断** inflight 子 Agent `ask()` | Localizer/Retriever 只读可 discard |
| 子 Agent L1 | token 传入 AgentLoop | model 请求 abort · tool 前检查 | Patcher write **等完回滚**；Verifier **kill sandbox** |
| 收尾 | `_repair_impl` finally | `RepairState.status=user_cancel` · release 写锁 · restore snapshot | trace `repair_cancelled` |

**冲突仲裁**（不靠辩论，Orchestrator + Blackboard）：

| 冲突 | 解决 |
|------|------|
| Blackboard 同 key 异 source | `resolve_conflict(prefer_localizer\|merge\|latest)` |
| 重复 suspect | 去重合并，保留高 confidence |
| 互斥 suspect | prefer_localizer 或 trace 标记 |
| patch vs verify | 回滚 + feedback → Patcher |

**终止**（`RepairState.status`）：

| 状态 | 触发 |
|------|------|
| `fixed` | verify 全绿 |
| `exhausted` | 达 max_retries |
| `failed` | 不可恢复错误 |
| `timeout` / `user_cancel` | deadline / 用户取消（终态枚举见 [§15](#15-自愈闭环)） |

**恢复**（衔接 [§11](#11-checkpoint-断点恢复与续跑)）：

| 粒度 | 机制 |
|------|------|
| 阶段续跑 | `repair_checkpoint.json` + `--resume-repair` |
| workspace | verify 失败 `restore_repo_snapshot` ✅ |
| 部分 phase | 从 failed phase 重跑，复用 Blackboard |


### 12.8 子问题拆分

> 大 issue 由 **Planner / RepairPlan** 驱动；子任务可串行 patch+verify，只读阶段目标并行。

```
Issue → RepairPlan.subtasks[] → 子问题 A/B… → Blackboard 汇总 → §12.7 冲突仲裁
```

| 策略 | 说明 |
|------|------|
| 按文件/模块 | 每 suspect_file 独立 patch，最后合并 diff |
| 按失败测试 | 每 failed test 一轮 feedback |
| 按 issue_type | import vs logic 分路（§12.1 动态裁剪） |
| 合并 | 顺序 apply patches + 单次 verify；中间 snapshot |

### 12.9 子 Agent 职责边界 · 共享工具

| Agent | 可写 | 专属工具 | 禁止 |
|-------|------|----------|------|
| Localizer | ✗ | stack/ast | write/patch/sandbox |
| Retriever | ✗ | git_* | write/patch |
| Patcher | ✓ | write/patch | sandbox |
| Verifier | ✗ | sandbox_verify | write/patch |

子 Agent **共享** 同一 `ToolGateway` 实例与 workspace；quota/session 分 Agent 计数（§12.6）。

### 12.10 Reviewer–Executor 防震荡 · parse 状态 reconcile

| 问题 | 策略 |
|------|------|
| Patcher↔Verifier 震荡 | 相同 failure 摘要冷却 · 降 temperature |
| parse 失败 corrupt state | **不 advance phase** · `agent_errors` + checkpoint |
| HITL | `--dry-run` · write/shell REPL 审批（§29.3） |


---

## 13. Skill

> **设计边界**  
> Skill = **触发正则 + 策略 metadata + 建议工具链 + 示例 patch**；与 Python 代码解耦，便于 eval 与面试展示「策略外挂」。  
> **模块**：`src/skills/*.yaml` · `Orchestrator._match_skill()` · prompt `[Skill 提示]` 块。  
> **现状 ✅**：4 个 Python skill 文件 · `trigger_pattern` 子串匹配 · pipeline 注入 suggested_tools。

### 13.1 YAML Schema

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | ✅ | 唯一标识 · trace `matched_skill` |
| `language` | ✅ | 与 `RepairPlan.language` 对齐 |
| `trigger_pattern` | ✅ | 正则；对 issue 全文 `re.search` |
| `priority` | 目标 | 多命中时 deterministic 选择 |
| `suggested_tools` | 推荐 | Localizer 工具序 hint |
| `example_issue` / `example_patch` | 推荐 | few-shot 注入 §13.3 |

**Gap**：无 pydantic/jsonschema 加载校验；无 `priority` 字段；glob 顺序 = 匹配顺序。

### 13.2 匹配算法

```
issue text → 遍历 skills/*.yaml
    → trigger_pattern 命中集合
    → (目标) 按 priority DESC，同 priority 取最长 pattern
    → 唯一 matched_skill → RepairPlan / prompt
```

| 场景 | 现状 | 目标 |
|------|------|------|
| 单命中 | ✅ 首个 glob 文件 | 同左 |
| 多命中 | 不确定 | priority + 最长 pattern（[§23.2](#232-skill-策略匹配)） |
| 未命中 | 无 skill 块 | default 策略 + trace 标记 |

### 13.3 注入与 Eval

- **Prompt 注入**（部分 ✅）：`suggested_tools` · `example_patch` → Orchestrator 模板 `[Skill 提示]`。  
- **命中率**（目标）：eval report 按 `matched_skill` × case_id 聚合。  
- **包目录**（目标）：`~/.fixloop/skills/` 覆盖项目 skills；与 §22 插件 entry_points 衔接。

### 13.4 海量 Skill 加载

> **问题**：Skill 数量上百时，**不能**将全部 YAML 正文塞进 prefix；需 **索引 + 按需加载 + 可选向量检索**。

**加载管线（目标）**：

```
启动 / repair 开始
    ↓
扫描 skill 源（见下表）→ 构建 SkillCatalog 索引（内存 + 可选落盘）
    ↓
prefix 仅注入：name · language · trigger 摘要 · priority · ~50 token 描述
    ↓
意图/ issue 匹配（§13.2 · §23.2）→ 加载 1–3 个 Skill 全文 → skills section
    ↓
L0 TierPolicy：未命中 skill 名不进 history 钉扎
```

| Skill 源 | 优先级 | 说明 |
|----------|--------|------|
| `{repo}/.agent/skills/` | 最高 | 项目覆盖 |
| `{repo}/src/skills/` | 高 | L2 内置（现状 4 个） |
| `~/.fixloop/skills/` | 中 | 用户全局 |
| entry_points `fixloop.skills` | 低 | 插件包 |

**Scale 策略**：

| 规模 | 策略 |
|------|------|
| N ≤ 20 | 全索引进 prefix；glob + regex 匹配 |
| 20 < N ≤ 100 | prefix 只放 **目录索引**；匹配后 lazy read YAML body |
| N > 100 | **embedding 检索** top-k skill（复用 §4.4 embed 基础设施）→ 再 regex 确认 |

**缓存**：`.agent/skill_index.json`（mtime · content_hash）；变更 invalidate prefix hash。

**Gap**：现状仅 4 skill · 全量 glob · 无 catalog · 无 lazy load · L0 `skill_mode=off`。

### 13.5 Skill 召回率 · 版本 · 质量 Rubric

| 指标 | 来源 | 用途 |
|------|------|------|
| skill precision/recall | `matched_skill` × eval case | §13 backlog · eval_report |
| content_hash swap | `.agent/skill_index.json` | 热更新无半读 |
| Rubric CI | yaml lint | 必填字段 · regex 合法 |

### 13.6 Skill 选择与 tool 联动

命中 skill 后 **`suggested_tools`** 约束 Gateway 可见集（可选 strict）；未命中走 default patcher 变体（§23.3）。


---

## 14. JSON 格式输出保证

> **设计边界**  
> Localizer / Patcher / Planner 产出 **结构化 JSON**；解析失败不能拖垮流水线。  
> **现状 ✅**：`parse_localizer_output` · `parse_patcher_output` · JSON extract from markdown fence。

**全栈（目标）**：

| 层 | 手段 |
|----|------|
| Provider | native JSON mode / `response_format`（per-agent 配置） |
| Parse | strict → json5 → regex extract |
| Validate | Pydantic schema · `validation_errors[]` |
| Retry | 附 schema 样例 + caret · ≤2 次 |
| Tool args | native `tool_calls.arguments` 走 `auto_validate` |

**Gap**：native JSON mode 未接 · tool args 校验未统一。


---

## 15. 自愈闭环

> **设计边界**  
> 闭环 = **verify 失败 → 结构化 feedback → Patcher 重试 → 回滚/再 apply → 再 verify**，最多 `max_retries` 轮。  
> **现状 ✅**：`state.feedback` · patch 回滚 · `max_retries=3` · VerifyStrategy 抽象。

### 15.1 Bad Case 采集 → 回归闭环

```
eval run fail → --record-failures → .agent/badcases/
    → 人工标注 expected_patch → 新 case_*.yaml
    → CI regression_check → fix_rate 门禁
```

| tag | 含义 |
|-----|------|
| `parse_fail` | JSON/schema 失败 |
| `wrong_file` | patch 目标错误 |
| `regression` | 引入新失败 |
| `timeout` | 阶段/整体超时 |


---

## 16. Docker 沙箱

> **设计边界（面试口径）**  
> 沙箱目标：**文件系统 · 网络 · 资源 · 权限**四维隔离。  
> **不防逻辑错误**：错误 patch 若仍通过 pytest，沙箱无法判定业务语义。  
> **有逃逸风险**：应文档化 threat model，**不声称「绝对安全」**。  
> **现状 ✅**：`network_mode=none` · `mem_limit`/`cpu_quota` · tar 传 `/code` · 单 Turn 结束 `destroy`。

### 16.1 文件系统隔离


### 16.2 网络隔离


### 16.3 资源隔离


### 16.4 权限降级



### 16.5 开销与逃逸回归




### 16.6 单 Turn 生命周期

### 16.7 工具执行沙箱分层

| 层级 | 工具 | 隔离 |
|------|------|------|
| **宿主机** | L1 `run_shell` · read/search/write | Gate + quota + approval |
| **容器** | L2 `sandbox_verify` | Docker network=none · read_only rootfs |

trace 字段 **`execution_tier=host|container`** 区分；verify 失败统一 kill + snapshot restore。


---

## 17. Patch 与 Verify


---

## 18. 敏感信息处理

> **设计边界**  
> 三层：**L1 运行时隔离** · **L2 输出脱敏** · **L3 存储/索引排除**。  
> **现状 ✅**：Shell env 白名单 · `redact_text` / `redact_artifact` · trace 写入前 redact。


---

## 19. 链路可观测

> **设计边界**  
> 每次 run 产出 **run_id + trace.jsonl + report.json**；L2 附加 **node_timings** · **agent token 汇总**。  
> **模块**：`run_store.py` · `src/repair/run_trace.py` · `callbacks.py` · `TaskState`。  
> **现状 ✅**：`_emit` 事件流 · Deterministic Replay · progress callbacks · per-agent token（PR #86）。

### 19.1 Run · Trace · Report

| 产物 | 内容 | 生命周期 |
|------|------|----------|
| `trace.jsonl` | 逐步事件 · tool · model · react_phase | `.agent/runs/{run_id}/` |
| `report.json` | token · cache · tool_steps · timings 汇总 | run 结束 |
| `repair_state.json` | L2 阶段快照（目标 checkpoint） | `.agent/repairs/` |

### 19.2 Repair · Agent 指标

| 指标 | 来源 | 用途 |
|------|------|------|
| `node_timings` | Orchestrator | localize/retrieve/patch/verify ms |
| `by_agent` tokens | 各 Agent TaskState | 成本 · eval 报告 |
| `agent_errors` | parse/gateway/verify | 降级依据 |

**Gap**：L2 `--verbose` 非结构化；Prometheus/Grafana（backlog）未接。

### 19.3 工具 · Gateway · Context 指标

| 指标 | 章 | 说明 |
|------|-----|------|
| `tool_rejections_by_gate` | §8 | 九道闸命中分布 |
| `permission_denied` count | §7 | Gateway 越权 |
| `sections` / `cuts[]` / budget | §3.2 | Context 裁剪可解释性 |
| `matched_skill` / `issue_type` | §23 | 意图识别 |
| memory 健康 | §4.5 | 条目数 · Dream 耗时 |

**纪律**：runtime metric 与 **eval metric**（§20.2）分层——前者单 run，后者 Case 集聚合。

### 19.4 核心运行时指标监控

> **目标**：每次 ask/repair 在 **`report.json` + `trace.jsonl`** 可还原 **成本（token/cache）· 延迟（TTFT）· 可靠性（retry/steps）**；REPL `/session` · eval 报告 · Prometheus 消费同一 schema。

**指标目录**：

| 指标 | 含义 | report / trace 字段 | 现状 |
|------|------|---------------------|------|
| **Context token（组装）** | `build()` 投影总 token · 各 section 用量 · 裁剪 | `total_tokens` · `token_usage` · `budget_cuts` · `prompt_budget` | 部分 ✅（`agent_loop._finalize_run`） |
| **Context token（八段）** | system/task/state/knowledge/tools/skills/memory/history | `context_sections{}`（目标） | Gap · 见 [§3.1](#31-设计原则) |
| **API input / output token** | Provider 计费 token | `input_tokens` · `output_tokens` · `api_calls` | 部分 ✅ |
| **Prompt cache 命中** | 前缀缓存读/写 token | `cache_read_tokens` · `cache_creation_tokens` · `cache_hit_rate` | Gap（usage 未汇总进 report） |
| **首字延迟 TTFT** | 请求发出 → 首个 content chunk/token | `ttft_ms` · trace `model_first_token` | Gap（非 streaming 仅记整包 latency） |
| **Completion 延迟** | 整包 model 往返 | `node_timings.model_ms` · `latency_stats` | 部分 ✅（CLI `latency_stats`） |
| **Parse / loop retry** | 输出格式错误重试 | `parse_retry_count` · trace `retry` | 部分 ✅（`_retry_count` 内存 · debug_retry.txt） |
| **Repair retry** | verify 失败重 patch | `RepairState.retry_count` · `max_retries` | ✅ L2 state |
| **Tool 步数** | 实际 execute_tool 次数 | `tool_steps` | ✅ report |
| **Model attempts** | 含 retry 的 model 调用次数 | `attempts` | ✅ report（≠ tool_steps） |

**Cache 命中率（目标公式）**：

```
cache_hit_rate = cache_read_tokens / max(cache_read_tokens + cache_creation_tokens, 1)
session 级汇总 → report.json + ModelClient.session_usage
```

**TTFT 采集点**（streaming 路径）：

```
trace: model_request_start
    → 首个 SSE/chunk 到达 → model_first_token {ttft_ms, agent, step}
    → 流结束 → model_complete {total_ms, output_tokens}
```

**与 §3.2 / §5.1 关系**：Context token 来自 `ContextManager.build()` metadata；cache 来自 Provider `usage` + `prompt_cache_key`；二者进同一 `report.json` 便于算 **「每千 token 成本 vs cache 节省」**。

**Eval 聚合**（见 [§20.2](#202-runner-与指标)）：Case 级 mean/p50 **context_tokens · cache_hit_rate · ttft_ms · tool_steps · retry_count** → `eval_report.json` · CI 基线对比。


---

## 20. 消融实验与评测

> **设计边界**  
> 消融 = 同一 Case 集上切换 **full / single / no_retriever** 等变体。  
> **模块**：`src/eval/runner.py` · `metrics.py` · `regression_check.py` · `fake_runner.py`。  
> **现状 ✅**：10 Case · 60 runs 正式结果 · `variants.py` · `compute_metrics()` · `patch_precision` 已实现。

### 20.1 Case 库


### 20.2 Runner 与指标

**核心指标**（`metrics.py` ✅）：

| 指标 | 公式 / 含义 | 面试 |
|------|-------------|------|
| `fix_rate` | fixed / total | 修没修好 |
| `patch_precision` | Σ(min_lines / max(actual_lines,1)) / n | **最小改动** |
| `first_attempt_rate` | retry=0 且 fixed | 一次成功率 |
| `regression_rate` | introduced_regression / total | 副作用 |
| `avg_retries` · `avg_duration_s` | 成本 | 效率 |

**分桶**：`by_type`（issue_type）· `by_difficulty` · `by_variant`（消融）。

**运行时指标聚合（目标，来自各 Case 的 agent report）**：

| 聚合字段 | 来源 | 用途 |
|----------|------|------|
| `avg_context_tokens` · `p50_context_tokens` | report `total_tokens` / sections | 窗长 · 压缩效果 |
| `avg_cache_hit_rate` | cache_read / (read+creation) | Prompt cache ROI |
| `p50_ttft_ms` · `p99_ttft_ms` | trace / report | 交互体验 |
| `avg_tool_steps` · `avg_attempts` | report | Agent 效率 |
| `avg_repair_retries` | `CaseResult.retry_count` | 自愈成本 |

**Gap**：`patch_equivalence_score`（expected diff）· Pass@k · CI 门禁阈值 · `--fake` 矩阵 · **运行时指标 eval 聚合未接**。

### 20.3 CI 门禁与基线

| 机制 | 模块 | 说明 |
|------|------|------|
| `regression_check.py` | CI | fix_rate / regression_rate 阈值 |
| `ci_baseline_report.json` | 基线 | 与 master 对比 |
| `eval --fake` | fake_runner | 零 API 冒烟 |

### 20.4 Agent 性能量化 · Judge · 检索质量

| 指标类 | 字段 | 模块 |
|--------|------|------|
| 修复 | fix_rate · patch_precision · regression_rate | `metrics.py` ✅ |
| 运行时 | avg_cache_hit_rate · p50_ttft_ms · avg_tool_steps | report 聚合（Gap） |
| 检索 | recall@k · precision@k | 标注集 + memory（Gap） |
| Judge | optional score+reason | eval 变体 `with_judge`（Gap） |
| 策略 A/B | `--retrieval-mode` | Orchestrator + eval（Gap） |


---

## 21. CLI · REPL（本地）

> **设计边界**  
> 用户在本机通过 **`python -m agent_runtime.cli`**（L1 REPL）与 **`python -m src.cli repair`**（L2 修复）驱动 FixLoop；会话与 trace 落盘于项目 `.agent/`。**不提供** HTTP `--serve` 或远程 API（见 [OUT_OF_SCOPE.md](OUT_OF_SCOPE.md)）。


---

## 22. 配置 · 插件 · 可靠性


---

## 23. 意图识别与路由

> **设计边界**  
> 在 **默认不调 LLM**（规则层）或 **可选轻量 LLM fallback** 下，将原始输入分类为可执行的 **`RepairPlan` / Skill / 会话动作**，并驱动后续 **Agent 裁剪**、**prompt 变体**与 **降级路径**。  
> **不含** Web NLU / 多租户意图；**不含** 自由对话 Agent。  
> **模块**：`src/orchestrator.py`（`_parse_issue` · `_match_skill` · `_classify_error`）· `src/skills/*.yaml` · `agent_runtime/features/memory/durable.py`（`_has_save_intent`）· `agent_loop.py`（`_gen_task_summary`）。  
> **现状 ✅（规则层）**：正则 Issue 解析 · YAML Skill `trigger_pattern` · 异常类型映射 · remember/记住 关键词 · CLI 打印 `识别: language, issue_type, suspect_files`。  
> **Gap**：无统一 `IntentResult` schema · Skill **首个命中**非 priority · `unknown` / 歧义 issue 无 LLM 升级 · 意图未结构化进 trace · REPL 无 intent router。

```
用户输入 (issue / REPL line)
        │
        ├─ L2 repair 路径 ──► _parse_issue() ──► RepairPlan
        │                         │
        │                         ├─ issue_type · language · suspect_files
        │                         └─ _match_skill() ──► matched_skill + suggested_tools
        │                                   │
        │                                   ▼
        │                         意图 → 路由（Agent 裁剪 · prompt 变体 · 降级）
        │
        └─ L1 会话路径 ──► save_intent / task_summary / (目标) REPL router
```

**与相邻章关系**

| 章 | 分工 |
|----|------|
| [§12](#12-multi-agent-编排) | **编排执行**：消费 `RepairPlan`，调度 Localizer∥Retriever → Patcher → Verifier |
| [§13](#13-skill) | **Skill 内容**：策略文本 · 示例 patch · **注入 Prompt** |
| [§5](#5-prompt) | **Prompt 组装**：issue_type 变体后缀的具体文案 |
| [§4.4](#44-召回与-context-投影) | **检索 query 意图**：`derive_embed_query()`，LLM 看全文、Embedding 看短 query |

### 23.1 L2 Issue 意图（Repair 入口）

> **`_parse_issue(issue)`**（`orchestrator.py` ✅）= repair 流水线的 **第一道闸**：纯 Python 正则，不调 LLM。

**输入**：CLI `--issue` / `issue.txt` / eval case 堆栈文本。

**输出**：`RepairPlan`（写入 `RepairState.repair_plan`）：

| 字段 | 规则来源 | 示例 |
|------|----------|------|
| `language` | 默认 `python`（待扩展检测） | `python` |
| `issue_type` | `_classify_error` · composite · config 启发式 | `type_error` · `import_error` · `composite` |
| `suspect_files` | `File "..."` · `at foo.py:42` · `Candidate source files:` | `["calculator.py"]` |
| `reasoning` | `file:line` 或 issue 前 200 字 | `calculator.py:42` |

**`issue_type` 枚举**（与 eval `metadata.yaml` 对齐）：

`type_error` · `import_error` · `attribute_error` · `logic_error` · `config_error` · `composite` · `test_failure` · `unknown`

**Gap**

| 场景 | 现状 | 目标 |
|------|------|------|
| 纯 pytest 失败（无 `XxxError`） | 易落 `unknown` | `test_failure` 启发式 |
| 多文件 stack | 部分 File 行可提取 | 全量 suspect_files + 行号 |
| 歧义 / 自然语言 issue | 仅 regex | 可选 light_client JSON fallback |
| 置信度 | 无 | `confidence` + `parser=rule\|llm` 进 trace |

### 23.2 Skill 策略匹配

> **`_match_skill(issue)`**（✅）遍历 `src/skills/*.yaml`，对 `trigger_pattern` 做 `re.search`；**首个命中即返回**（顺序依赖 glob，非 deterministic priority）。

| 概念 | 说明 |
|------|------|
| **触发** | Issue 文本匹配 YAML `trigger_pattern` |
| **产出** | `suggested_tools` · `example_patch` · skill `name` → 注入 Orchestrator prompt（[§13](#13-skill)） |
| **冲突** | 多 skill 同时命中 → 待 **priority + 最长 pattern**（backlog §13 / §23） |

**面试怎么说**：Skill 匹配是 **意图识别的策略层**——把 `issue_type` 细化为可执行的 tool 链建议，与 §13 的 Prompt 注入解耦。

### 23.3 意图 → 编排路由

> 识别结果 **不直接调 LLM**，而是驱动 Orchestrator **裁剪与 prompt 分支**（纯 Python）。

```
RepairPlan.issue_type + matched_skill
        │
        ├─ Agent 裁剪：import 简单 case 跳过 Retriever；composite 强制四 Agent
        ├─ Prompt 变体：type_error / import_error / config 不同 patcher 后缀（§5）
        ├─ 降级：Localizer 空 → _fallback_suspects_from_plan（§10.2）
        └─ 大 issue：Planner → RepairPlan.subtasks（§12.8）
```

| `issue_type` | 路由策略（目标） |
|--------------|------------------|
| `import_error` | 优先 stack + import 行；Retriever 可降级 |
| `type_error` | stack_parse + ast_parse 序（§6.2） |
| `composite` | 强制 Retriever · subtasks 拆分 |
| `config_error` | suspect 含 `pyproject.toml` 等 |
| `unknown` | 不跳过 Agent；可选 LLM 重分类 |

### 23.4 L1 会话意图（REPL / Memory）

| 意图 | 检测 | 动作 |
|------|------|------|
| **remember / 保存** | `_has_save_intent()` ✅ | `promote_durable_memory()` → durable topic |
| **任务摘要** | `_gen_task_summary()` ✅ | working `task_summary` → 供 §4.4 检索 |
| **repair vs ask** | （Gap）无 REPL router | `/repair` 或自然语言分流到 L2 / L1 |
| **cancel** | Ctrl+C / `/cancel` | 见 [§2.1](#21-用户中断与取消) |

### 23.5 检索 Query 意图

> 与 Issue **分类**不同：此处指 **Embedding 用的短 query**，避免对全文 stack 做 `encode`。

**链路**：`task_summary` → 规则抽取（issue_type · stack tail · paths）→ head/tail 截断 → `derive_embed_query()`。

详见 [§4.4](#44-召回与-context-投影) · backlog [§4.4](../bonus.md#44-召回与-context-投影)。

### 23.6 可观测与评测

| 观测点 | 字段（目标） |
|--------|----------------|
| CLI verbose | `[Orchestrator] 识别: ...`（现状 ✅ stdout only） |
| trace / report | `repair_plan` 快照 · `matched_skill` · `issue_type` · `intent_parser` |
| eval | `case_adv_ambiguous_*` · `case_adv_misleading_type_*` → 期望 `exhausted` 或 clarify |

---

## 24. 压测与容量


---

## 25. 演示 · 文档 · 测试


---

## 26. 输出质量 · 幻觉探针 · Judge Eval

> **设计边界**  
> 在 **verify 之前/之后** 用规则 + eval Case 拦截 **faithfulness 失败**（改错文件 · 捏造 stack）；可选 **LLM-as-Judge** 仅 eval 变体。  
> **模块**：`patch_applier.py` · `output_parsers.py` · `src/eval/cases/` · `metrics.py`。

| 层 | 手段 |
|----|------|
| Sanity | 空 diff · 全删 · `ast.parse` 预检 |
| 范围 | patch ⊆ suspect_files ∪ related_tests |
| Eval | `case_adv_hallucination_*` |
| Judge | opt-in judge_client · 不进 repair 主路径 |

---

## 27. 检索增强 · Embedding · 查询改写

> **设计边界**  
> 代码修复 **Grep/Read 优先** — backlog 主条目 [§6.4](../bonus.md#64-工具分发--参数校验--retriever-路径)；embed/知识卡片 [§4.8](../bonus.md#48-用户画像--遗忘--embedding-迁移)。  
> 本节 backlog 仅：`--retrieval-mode` · HyDE/query 改写（[bonus §27](../bonus.md#27-检索增强--embedding--查询改写)）。

---

## 28. MCP · Function Calling 适配

> **设计边界**  
> FC/Gateway backlog 主条目 [§7.4](../bonus.md#74-function-calling-执行环)；skill_mode [§13.4](../bonus.md#134-海量-skill-加载)。  
> 本节 backlog 仅：MCP stdio shim（[bonus §28.1](../bonus.md#281-mcp-server-shim可选)）。

---

## 29. Agent 范式落地

> **设计边界**  
> Plan/ReAct/stall/reconcile 见 [§12.8](../bonus.md#128-子问题拆分) · [§2.3](../bonus.md#23-loop-engineering--middleware--防死循环) · [§12.10](../bonus.md#1210-reviewerexecutor-防震荡--parse-状态-reconcile)。  
> 本节 backlog 仅：HITL dry-run + 审批（[bonus §29](../bonus.md#29-agent-范式落地)）。

---

*设计说明 · 本地运行 · base `master` @ PR #87 · 558 tests · 待办见 [bonus.md](../bonus.md)*
