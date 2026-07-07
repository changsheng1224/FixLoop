# FixLoop Bonus 功能探索（筛选版）

> 原 Layer 1/2 分册 bonus 文档 **已合并去重并经条目筛选**（含删减后二次整理：交叉引用替代重复条目、canonical 章内保留细节）。全文档 **25 章（§1–§25）**；原 §13 State/Blackboard 已并入 §12 Multi-Agent。  
> 基线：`master` @ PR #85 · `agent_runtime/` + `src/` · **476 tests** · 覆盖率 **80%**。  
> 格式：**[P?] [C:复杂度 I:面试/展示价值] 标题**：简要方案。标注 **✅** 表示已有基础实现，条目为增强。

---

## 目录

| 章 | 能力域 | 主要模块 |
|----|--------|----------|
| [1](#1-agent-运行时) | Agent 运行时 | `runtime.py` · `bootstrap.py` · `config.py` |
| [2](#2-agent-loop--react) | Agent Loop / ReAct | `agent_loop.py` · `callbacks.py` |
| [3](#3-context-工程) | Context 工程 | 五 section · 预算 · L0–L5 |
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
| [21](#21-cli--repl--api) | CLI / REPL / API | `cli.py` |
| [22](#22-配置--插件--可靠性) | 配置 / 插件 / 可靠性 | `fixloop.yaml` · entry_points |
| [23](#23-web-产品化与多用户) | Web 产品化 | REST · SSE · 租户隔离 |
| [24](#24-压测与容量) | 压测 / 容量 | Locust/k6 · 沙箱池 |
| [25](#25-演示--文档--测试) | 演示 / 文档 / 测试 | Demo · ADR · pytest |

---

## 1. Agent 运行时

> **设计边界**  
> 运行时 = **单 Agent 实例**的生命周期管理：session、tools registry、config、workspace 锚定、与 ModelClient 交互。  
> **不含** L2 修复编排；L2 通过 `repair_factory` 创建多个运行时实例。  
> **现状 ✅**：`Agent.ask()` · `execute_tool()` · session 持久化 · prefix hash · dry_run 透传。

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Agent 池化 / 预热**：repair 启动预建 Localizer/Retriever 实例，复用 prefix hash 与 memory 投影，降首轮 latency
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 统一 token 会计**：session 级 `input_tokens` / `output_tokens` / `cache_read` 汇总进 `report.json`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] REPL 热重载**：`/config max_steps=10`
- **[P2] [C:⭐ I:⭐⭐⭐] workspace 切换检测**：`cwd` 变更时 invalidate prefix hash + working memory recent_files
- **[P2] [C:⭐ I:⭐⭐⭐] `agent.register_tool` 动态扩展**
- **[P2] [C:⭐ I:⭐⭐] 多 Provider fallback 链**：Anthropic 熔断 → 备用 OpenAI-compatible endpoint（需显式 opt-in）
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 统一 logger + `--log-level`**

---

## 2. Agent Loop / ReAct

> **设计边界**  
> ReAct = **Reasoning → Acting → Observation → Recording** 四阶段循环，直至 `<final>` 或 `max_steps`。  
> 双路径：**文本 XML/JSON 解析** + **原生 `tool_use` block**（provider 支持时）。  
> **现状 ✅**：单步循环 · 工具结果写 history · parse 失败指数退避 · `TaskState.tool_steps` 计数。

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

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] CancellationToken 全链路**：AgentLoop · ModelClient · ToolExecutor 共享；CLI Ctrl+C · Web `POST /cancel` 置位
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 协作式 cancel 检查点**：每 step 开始前 · model 返回后 · **每次 `execute_tool` 前**检查；已 cancel 则不再调度新 tool
- **[P1] [C:⭐⭐ I:⭐⭐⭐] TaskState.user_cancel**：`status=stopped` · `stop_reason=user_cancel` · trace 含 phase + in-flight tool
- **[P1] [C:⭐ I:⭐⭐⭐] cancel 后 workspace 一致性**：write 类依赖 Gate 8/9 snapshot diff + restore（[§8](#8-工具安全闸口)）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 流式模型 cancel**：chunk 循环内检查 token，立即 abort 并关闭连接
- **[P2] [C:⭐ I:⭐⭐⭐] REPL `/cancel` 或二次 Ctrl+C**：向当前 `AgentLoop` 实例下发 cancel，不杀整个进程

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 补全 AgentLoop 触发**：`on_step_start` / `on_final_answer` / native 路径统一 invoke
- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 批量工具调用**：多 `<invoke>` / JSON array / native 多 tool block 同轮一次 Acting
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 单步工具超时**：`concurrent.futures` 防 hang
- **[P1] [C:⭐⭐ I:⭐⭐⭐] ✅ retry 指数退避**：可配置 `--retry-max-delay`
- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 显式 ReAct 阶段 trace**：每步 emit `react_phase: reasoning|acting|observation|recording`，便于回放与 Demo
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 单步 wall-clock 超时**：整步（含 model + tools）超 `step_timeout` 则 `stop_reason=step_timeout`
- **[P2] [C:⭐ I:⭐⭐⭐] stop_reason 枚举**：`final|step_limit|circuit_breaker|parse_fail|user_cancel`
- **[P2] [C:⭐ I:⭐⭐⭐] 空转检测**：连续 N 步无 `affected_paths` 且无 final → 提前终止 + 提示 replan
- **[P2] [C:⭐ I:⭐⭐⭐] 解析失败 recovery prompt**：附「上一 valid 输出片段 + 错误位置 caret」，减少 blind retry
- **[P2] [C:⭐ I:⭐⭐] final_answer  schema 校验**：可选 JSON mode final（如 repair 子任务），失败则回到 Acting
- **[P3] [C:⭐⭐ I:⭐⭐⭐] CoT 提取**：thinking 块剥离后再进 history

---

## 3. Context 工程

> **一句话（面试）**：Context 不是「把能塞的都塞进去」，而是在固定 window 内做 **canonical 真相源 + prompt 投影**——`ContextManager.build()` 按 **五 section + tiktoken 预算** 组装，**user 永不裁**、tool 结果 **L1 截断**、history **L5 摘要或规则压缩**；Memory 只以 section 注入（[§4](#4-分层记忆)）。  
> **模块**：`agent_runtime/context_manager.py` · `prompt_prefix.py` · L2 Orchestrator 手工拼 prompt（目标复用同一 `TokenBudget`）。

### 3.1 设计原则

| 概念 | 含义 | 面试怎么说 |
|------|------|------------|
| **Context** | 为**当前任务**组装的全部信息 | 「当轮视图，会裁剪」 |
| **Prompt** | 一次 API 的 messages = **prompt_projection** | 「build() 产物，不是 session 全量」 |
| **canonical** | 会话真相源，压缩不修改原文 | 「history 只追加，丢信息可回滚重投影」 |
| **Memory** | 跨轮存储，经 section 注入 | 「存全量、读子集，见 §4」 |

**三条纪律**

1. **User 意图不可丢**：`request` section 永不 `fit()` 裁剪（现状 ✅）。  
2. **稳定 prefix 利于 cache**：workspace + rules + tool signatures 放 `prefix`（`prompt_cache_key = prefix.hash` ✅）。  
3. **压缩分级**：越贵越晚触发——L1 规则截断 → L5 LLM 摘要；canonical 不动。

```
session["history"] + session["memory"]     ContextManager.build(user_message)
（canonical，内存态）                           │
        │                                      ├─ prefix    ← prompt_prefix
        │                                      ├─ memory    ← Working（§4）
        └──────────────────────────────────────├─ relevant  ← 混合检索（§4.4）
                                                 ├─ history   ← 压缩管线
                                                 └─ request   ← user 全文 ✅
                                                 → prompt_projection + metadata
```

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] history canonical JSONL**：`.agent/history.jsonl` 只追加；`build()` 只读投影

### 3.2 五 Section 组装与 Token 预算

> **`build()` 流水线**（现状 ✅）：收集五段文本 → tiktoken 计数 → 按顺序填充，**总预算超则 `fit()` 当前 section** → 附加 `## 当前任务` + user 全文。

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

> **Gap（诚实讲）**：`BUDGET_*` 常量已定义，但 `add_section` 尚未对每 section 单独 enforce 硬顶——仅 TOTAL 双限。

**Prompt Cache**（现状 ✅）：`metadata["prompt_cache_key"] = prefix.hash` 透传 ModelClient。

- **[P1] [C:⭐ I:⭐⭐⭐⭐] ✅ tiktoken 精确计数**：替代字符估算，中文误差 <5%
- **[P1] [C:⭐ I:⭐⭐⭐] 多模型 tokenizer 切换**：`encoding_for_model(model)`；未知 fallback + warn
- **[P1] [C:⭐ I:⭐⭐⭐⭐] cache 命中率进 report**：`cache_read_tokens` / `cache_creation_tokens`
- **[P2] [C:⭐ I:⭐⭐⭐] prefix 分段 hash**：tools 段与 rules 段分开 cache
- **[P2] [C:⭐ I:⭐⭐⭐] build() metadata 进 trace**：`sections` · `cuts[]` · `total_tokens` / `budget`
- **[P1] [C:⭐ I:⭐⭐⭐] Tools 仅注入启用集**：prefix 只含本轮 `_tool_names` 签名
- **[P2] [C:⭐ I:⭐⭐⭐] Skills 索引进 prefix、全文按需**：Orchestrator 匹配后注入 user/system
- **[P2] [C:⭐⭐ I:⭐⭐⭐] User Message 模板化**：Jinja 渲染任务/引用/格式；与 system 分离

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

- **[P1] [C:⭐ I:⭐⭐⭐] ✅ 按工具类型截断 + 重要行优先**：token 级；Error/Fail/路径行排在截断前部
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] ✅ LLM 摘要 + 三级降级**：L5 在 L1–L4 之后；LLM → fallback 最近 8 条；未触发 L5 时仍可用规则 `_compress_old_entries`
- **[P1] [C:⭐ I:⭐⭐⭐] 摘要缓存持久化**：`_summary_cache` 落盘 `.agent/summary_cache/`（现状内存 dict）
- **[P2] [C:⭐ I:⭐⭐⭐] ✅ 规则路径合并 read_file**：`_compress_old_entries`（L2–L4 未触发时的旧段降级）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 增量摘要**：在 `[Earlier summary]` 上追加，避免每轮全量重摘要
- **[P2] [C:⭐ I:⭐⭐⭐] ✅ Error/Traceback 强制保留**：L2–L4 `PROTECTED_KEYWORDS` / `has_protected_tool_content`
- **[P2] [C:⭐ I:⭐⭐⭐] ✅ L2–L4 分级实现**：Snip / Microcompact / Collapse + 百分比阈值 55/70/82%
- **[P1] [C:⭐⭐ I:⭐⭐⭐] ✅ 最近 20k token 保护区**：tiktoken 计量 · turn 级 · 跨边界整 turn 保护 · L2–L4 豁免
- **[P2] [C:⭐ I:⭐⭐] KEEP_RECENT_HISTORY 可配置**：默认 6；eval 长会话调大
- **[P2] [C:⭐ I:⭐⭐⭐] ✅ turn_id 标记**：`Agent.record()` 打标 · 当前 turn 内事件不压缩
- **[P1] [C:⭐ I:⭐⭐⭐] native 路径接入全管线**：`chat_with_native_tools` history 走 L0–L5
- **[P2] [C:⭐ I:⭐⭐⭐] 压缩阈值 yaml 外置**：55/70/82/100% 可配置

### 3.4 L2 Repair 与 Memory 衔接

> L1 用 `ContextManager.build()`；L2 Orchestrator **手工拼 prompt**，须复用同一预算纪律与钉扎区。

| 路径 | 组装方式 | Memory |
|------|----------|--------|
| L1 Agent | `build()` 五 section | `memory` + `relevant`（§4.4；**检索 query ≠ user 全文**） |
| L2 Localizer/Patcher | Orchestrator prompt 模板 | issue/stack **钉扎**；suspects/tests `fit()` |

- **[P1] [C:⭐ I:⭐⭐⭐⭐] issue/stack 钉扎区**：Localizer/Patcher 全文保留 error trace
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 共享 TokenBudget 库**：L2 调 `fit()` / tiktoken（现状 L2 未接入，P1 目标）
- **[P2] [C:⭐ I:⭐⭐⭐] 分 Agent 预算表**：Localizer 2k / Retriever 3k / Patcher 4k / Verifier 1k
- **[P2] [C:⭐ I:⭐⭐⭐] 钉扎区 registry**：yaml 声明永不裁剪字段（issue · stack · suspect.file_path）

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

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] ✅ task_summary**：light_client 一句话摘要；失败降级截断 user 文本
- **[P2] [C:⭐ I:⭐⭐⭐] ✅ freshness hash 校验摘要**：`mtime:size` 变则 `invalidate_file_summary`
- **[P2] [C:⭐ I:⭐⭐⭐] recent_files 显式 LRU + last_access**：超 `MAX_RECENT_FILES` 淘汰最旧
- **[P2] [C:⭐ I:⭐⭐⭐] episodic kind 分类检索**：error/decision/observation 分权重
- **[P2] [C:⭐⭐ I:⭐⭐⭐] episodic → durable 晋升**：`kind=decision` 且多次被检索 → 自动 promote

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

- **[P1] [C:⭐ I:⭐⭐⭐⭐] ✅ reject_durable_reason 写入闸口**：空/过短/过长/API key/GitHub token 拒绝落盘
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Candidate schema + 规则/LLM 双路抽取**：LLM 仅填规划 topic/key，禁止自由建库
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 冲突状态机**：`None|Equivalent|Override|Invalid` + 权威序；低权威不覆盖高权威
- **[P2] [C:⭐ I:⭐⭐] 互斥 key 版本链**：同 topic 语义互斥（如 Python 版本）保留 history 链而非覆盖

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

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] embed_query 与 user request 分离**：`_get_relevant` 用 `derive_embed_query()`（优先 `task_summary` → 规则抽取 → head/tail），禁止对全文 user/issue 直接 `encode`；keyword 路径不受 256 token 限
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] EMBED_MAX_TOKENS + head/tail 截断**：按模型 `max_seq_length` 配置；stack 类文本保留 **Traceback 尾段 + 最后一帧 File/line**
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] ✅ 混合检索 merge**：keyword + semantic 去重合并；durable 子串 top-2
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 语料 chunk + max-pool 检索**：durable/precedent 超 `EMBED_MAX_TOKENS` 按段 embed，query 与任一段 max cosine
- **[P2] [C:⭐ I:⭐⭐⭐] embedding 模型可插拔**：`FIXLOOP_EMBED_MODEL` + 每模型 `max_seq_length` 元数据；切换时 rebuild index
- **[P2] [C:⭐ I:⭐⭐⭐] embedding 磁盘缓存**：对 **normalize 后**文本 content_hash → `.agent/embed_cache/`

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
| 用户 | `user_id` | Web 多租户（[§23](#23-web-产品化与多用户)） |
| 会话 | `session_id` | `session.json` + working/episodic |

- **[P2] [C:⭐⭐ I:⭐⭐⭐] 记忆 GC + episodic 上限**：durable LRU；episodic 超 `MAX_EPISODIC_NOTES` 淘汰
- **[P2] [C:⭐ I:⭐⭐⭐] 置信度时间衰减**：`confidence *= decay^(days_since_seen)`，低于阈值不参与召回
- **[P2] [C:⭐ I:⭐⭐] topic 级 TTL**：preferences 长 TTL；session-notes 短 TTL
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Memory Dream 后台任务**：idle / repair 结束；去重 · 过期 · index 重建
- **[P2] [C:⭐ I:⭐⭐⭐] 健康 metric 进 report**：条目数、重复率、平均 confidence、Dream 时间
- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 三层路径隔离**：API 禁止 path 遍历读他人 memory

### 4.6 L2 Repair 记忆桥接

> **闭环**：读 precedent → 辅助 Patcher；写 precedent → 反哺 L1 Durable。`RetrievedContext.similar_fixes` 字段 ✅，读写管道为目标 P1。

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] repair precedent 读写一体**：启动读 topics → `similar_fixes`；`status=fixed` → upsert（issue 类型 + patch 摘要 + case_id）
- **[P1] [C:⭐ I:⭐⭐⭐] similar_fixes 置信度闸口**：semantic score < threshold 不注入 Patcher
- **[P2] [C:⭐ I:⭐⭐] 不信任记忆覆盖 suspect**：先例仅 hint，Localizer 仍走 stack/AST

### 4.7 运维与面试要点

**REPL**

- **[P1] [C:⭐ I:⭐⭐⭐] `/memory` 真实输出**：分层渲染 working / episodic / durable index
- **[P2] [C:⭐ I:⭐⭐⭐] `/memory forget`**：按层或 topic 删除；Dream 建议 forget 候选

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

---

## 5. Prompt

> **设计边界**  
> L1 prefix = workspace + rules + tool signatures（稳定段利于 cache）；L2 各 Agent 外置 txt。  
> **现状 ✅**：`prompt_prefix.hash` · dry_run/approval rules · 角色 prompt 文件。

- **[P1] [C:⭐ I:⭐⭐⭐⭐] Cache 命中率 REPL 展示**：`/session` 显示 cache_hits/misses（指标采集见 [§3.2](#32-五-section-组装与-token-预算)）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] few-shot / rules 外置**：`.agent/examples.md`、`.agent/rules.md`
- **[P2] [C:⭐ I:⭐⭐⭐] 分 issue 类型 prompt 变体**：ImportError 与 logic_error 用不同 patcher 后缀

---

## 6. Agent Tool

> **设计边界**  
> Tool = **dataclass 参数 schema + 执行函数 + 注册 metadata**。  
> L1 通用工具在 `agent_runtime/tools.py`；L2 领域工具在 `src/tools/`。  
> **现状 ✅**：`auto_schema` / `auto_validate` · read/search/write/patch/shell · sandbox_verify · ast_parse · stack_parse。

### 6.1 L1 通用工具

- **[P1] [C:⭐ I:⭐⭐⭐] search 正则模式**：`regex=True` 用 `re.search` 替代子串匹配
- **[P1] [C:⭐ I:⭐⭐⭐] write_file 原子写**：先写 `.tmp` 再 `replace`
- **[P2] [C:⭐ I:⭐⭐⭐] search 结果上限**：`max_results` + 截断提示
- **[P2] [C:⭐ I:⭐⭐] list_files glob / depth**：`pattern="*.py"`、`depth=1` 限制递归

### 6.2 注册与 L2 领域工具

- **[P1] [C:⭐ I:⭐⭐⭐] Retriever 规则快路径**：`--fast-retrieve` 跳过 LLM
- **[P2] [C:⭐ I:⭐⭐⭐] 工具组合 ToolGroup**：`inspect_file` = read_file + ast_parse 原子组合，占 1 次 quota
- **[P2] [C:⭐ I:⭐⭐⭐] Localizer 工具顺序**：stack_parse → ast_parse；违规 warn
- **[P2] [C:⭐ I:⭐⭐⭐] ast_parse 局部解析**：仅 suspect 行附近 AST

---

## 7. ToolGateway

> **设计边界**  
> ToolGateway = Agent 与 ToolExecutor 之间的**权限中间件**；Agent 无法绕过。  
> **现状 ✅**：`make_tool_policy()` · Localizer 无 write · Verifier 有 sandbox 无 patch · Patcher 有 write 无 sandbox。

- **[P2] [C:⭐ I:⭐⭐⭐] ToolGateway 越权审计**：`permission_denied` → trace / agent_errors
- **[P2] [C:⭐ I:⭐⭐⭐] 双层拒绝语义**：Gateway=角色不允许；Executor=参数/配额不允许；`tool_error_code` 区分 `permission_denied` vs `quota_exceeded`

---

## 8. 工具安全闸口

> **设计边界**  
> 九道闸口顺序执行，**任一失败返回 `ToolExecutionResult`，不抛异常**。  
> **现状 ✅**：白名单 → 存在 → validate → quota → duplicate → dry_run → approval → pre snapshot → exec → post snapshot。

- **[P1] [C:⭐ I:⭐⭐⭐] 审批时 diff 预览**：write_file / patch_file 审批时显示 patch 前后片段
- **[P1] [C:⭐ I:⭐⭐⭐] Gate 5 语义 duplicate**：同 tool + 同 path 参数即使 text 不同也视为重复 read
- **[P2] [C:⭐ I:⭐⭐⭐] Gate 7 分级审批**：write/patch 需 ask，read/search auto；Web UI 统一审批队列
- **[P2] [C:⭐ I:⭐⭐⭐] 符号链接逃逸检测**：`ToolContext.resolve` 二次 `resolve()` 校验
- **[P2] [C:⭐ I:⭐⭐⭐] 闸口拒绝统计**：`tool_rejections_by_gate` 进 report，面试展示「哪道闸最常被触发」

---

## 9. 硬上限与工具配额

> **设计边界**  
> **硬上限** = 不可协商的计数上限，达限即 `quota_exceeded`。  
> **现状 ✅**：writes ≤20 · shell ≤10 · total ≤50（单 session）；`max_steps` · `max_retries` · sandbox mem/cpu。

- **[P1] [C:⭐ I:⭐⭐⭐] 分 Agent 配额**：Patcher write 与 Localizer read 分开计数，防 Retriever 耗尽 total
- **[P2] [C:⭐ I:⭐⭐⭐] context token 硬顶**：`HARD_CAP=8000` 仍超则拒绝 ask（见 [§3.2](#32-五-section-组装与-token-预算)）

---

## 10. 限流 · 熔断 · 降级

> **设计边界**  
> **限流** = 请求速率控制；**熔断** = 连续失败断开；**降级** = 备用路径降能力保可用。  
> **现状 ✅**：`CircuitBreaker`（5 失败 / 30s 恢复）· rg→Python grep · Docker→host pytest · LLM 摘要失败→规则 trim。

### 10.1 模型 API

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐] Ollama / OpenAI streaming**：SSE/chunk 增量解析，REPL 实时输出
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 熔断事件进 trace**：`circuit_opened` / `half_open_probe` / `circuit_closed`
- **[P2] [C:⭐ I:⭐⭐⭐] 分 Provider / 分模型熔断**：Anthropic 与 Ollama 独立 breaker 状态
- **[P2] [C:⭐ I:⭐⭐⭐] Retry-After + jitter**：429 退避加随机抖动
- **[P2] [C:⭐ I:⭐⭐⭐] 半开成功阈值**：连续 2 次 probe 成功才 CLOSED，防抖动
- **[P3] [C:⭐⭐ I:⭐⭐] HTTP keep-alive**：同 session 连接复用

### 10.2 修复流水线降级

- **[P1] [C:⭐ I:⭐⭐⭐] Retriever 降级规则检索**：LLM 超时 → 堆栈文件名 + rg；失败时补 `related_tests`（主动 `--fast-retrieve` 见 [§6.2](#62-注册与-l2-领域工具)）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Multi-Agent 降级 Single-Agent**：verify 连续失败后 `degraded_mode` + baseline


---

## 11. Checkpoint 断点恢复与续跑

> **设计边界**  
> L1 checkpoint = 跨 **ask/session** 恢复；L2 repair checkpoint = 跨 **repair 阶段** 恢复。  
> **现状 ✅**：`create_checkpoint` · `evaluate_resume_state` · key_files freshness hash。

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 每 tool 步 checkpoint**：`trigger=step_end`；`--resume` 从最后成功步继续
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] L2 阶段 checkpoint**：`repair_checkpoint.json` · `--resume-repair <run_id>`
- **[P2] [C:⭐ I:⭐⭐⭐] blocker / next_step 字段 LLM 填充**：checkpoint 含「卡在哪、建议下一步」
- **[P2] [C:⭐ I:⭐⭐] Web repair 断点续跑**：用户刷新页面从 Redis 拉 phase + 继续 SSE
- **[P2] [C:⭐ I:⭐⭐] SessionStore 损坏恢复**：`.bak` 或跳过告警
- **[P1] [C:⭐⭐ I:⭐⭐⭐] cancel 时写 checkpoint**：`trigger=user_cancel` · 含最后成功 tool step + `in_flight_tool`；支持 `--resume` 从安全步继续

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

- **[P2] [C:⭐⭐ I:⭐⭐⭐] 动态 Agent 裁剪**：简单 import 跳过 Retriever；composite 强制四 Agent
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Planner Agent**：输出 `RepairPlan` JSON，Orchestrator 按 plan 调度

### 12.2 流水线编排

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

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 分阶段超时**：localize / patcher / verify 独立 timeout
- **[P2] [C:⭐⭐ I:⭐⭐] asyncio 流水线（可选）**：ThreadPool 已满足 M6

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

- **[P1] [C:⭐ I:⭐⭐⭐] 状态机显式枚举**：`phase: localize|retrieve|patch|verify|done|failed` 与 `status` 终态分离（完整终态见 [§15](#15-自愈闭环)）
- **[P2] [C:⭐ I:⭐⭐⭐] repair 落盘**：`.agent/repairs/{id}/repair_state.json` + timings
- **[P2] [C:⭐ I:⭐⭐⭐] L1/L2 State 关联字段**：`RepairState.run_id` ↔ 各 Agent `TaskState.run_id` 进 trace 便于串联

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

- **[P2] [C:⭐⭐ I:⭐⭐⭐] Agent 产物 JSON Schema 校验**：`output_parsers` 前 pydantic/jsonschema 校验，失败进 `agent_errors`

### 12.5 Blackboard 与 Agent 通信

> **Blackboard**（`src/blackboard.py` ✅）= repair 内的 **KV 交换板**，补 `RepairState` 固定 schema 装不下的中间结论。  
> **纪律**：同 key **同 source 可覆盖**；**异 source 拒绝覆盖**并记入 `_conflicts`，由 Orchestrator 仲裁。

| API | 行为 |
|-----|------|
| `write(key, value, source_agent, ttl?)` | 成功 True；冲突 False |
| `read(key)` | 单条；TTL 过期自动删 |
| `read_related(prefix)` | 前缀批量读（如 `suspect/`） |
| `snapshot()` | entries + conflicts 副本 |
| `resolve_conflict(key, winner_source)` | 手动仲裁 |

**与 RepairState 分工**：

| 放 RepairState | 放 Blackboard |
|----------------|---------------|
| 各 Agent **最终产物**（suspects · patches · verify） | 中间假设、多源候选、子任务命名空间 |
| CLI/eval 需要的**稳定 schema** | TTL 短的 scratch · 并行 phase 暂存 |
| checkpoint / resume 必需字段 | 冲突待仲裁条目 |


- **[P2] [C:⭐ I:⭐⭐] 前缀订阅**：prompt 构建时 `read_related("suspect/")` 批量注入，替代 Orchestrator 手工拼块

### 12.6 并发与一致性

> **读可并行、写必串行、验证在快照之后**。

| 场景 | 策略 | FixLoop |
|------|------|---------|
| Localizer ∥ Retriever read | 只读；无写者 | ✅ ThreadPool(2) |
| 多 Agent 同文件 write | **禁止**；仅 Patcher 可写 | ✅ ToolGateway |
| verify 前污染 | **repo_snapshot** + restore | ✅ 已有 |
| 同 repo 多 repair | 写锁 / 独立 temp workspace | 目标 Web [§23](#23-web-产品化与多用户) |
| 重复 read | Gate 5 duplicate | [§8](#8-工具安全闸口) |

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 阶段级读写锁**：localize/retrieve 共享读；Patcher 独占写
- **[P1] [C:⭐ I:⭐⭐⭐] workspace 写窗口单飞**：同一时刻最多一个 patch phase
- **[P2] [C:⭐ I:⭐⭐⭐] 分 Agent 独立 session/quota**：并行 Agent 不共享 L1 session（[§1](#1-agent-运行时)）
- **[P2] [C:⭐ I:⭐⭐] concurrent tool 硬顶**：并行 subprocess 上限（[§9](#9-硬上限与工具配额)）

### 12.7 冲突 · 终止 · 恢复

**用户 cancel 级联**（L2 Multi-Agent）：

| 层级 | cancel 入口 | 传播 | 工具执行中 |
|------|-------------|------|------------|
| Web / CLI | `POST /cancel` · Ctrl+C | Orchestrator `cancel_event.set()` | 见 [§2.1](#21-用户中断与取消) |
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

- **[P1] [C:⭐⭐ I:⭐⭐⭐] Orchestrator 冲突仲裁 API**：`resolve_conflict(key, strategy=...)`
- **[P2] [C:⭐ I:⭐⭐⭐] Localizer∥Retriever 去重**：同 file_path + 行号合并
- **[P2] [C:⭐ I:⭐⭐] 冲突进 trace/report**：`blackboard_conflicts[]`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 降级 Single-Agent 最后一搏**：见 [§10.2](#102-修复流水线降级)

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

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] RepairPlan.subtasks schema**：`[{id, goal, suspect_files, depends_on}]`
- **[P2] [C:⭐ I:⭐⭐⭐] composite Case 驱动**：eval composite 验证拆分+合并
- **[P2] [C:⭐ I:⭐⭐] 子问题失败隔离**：单 subtask exhausted → partial fixed

---

## 13. Skill

> **设计边界**  
> Skill = **触发正则 + 策略文本 + 建议工具链 + 示例 patch**；与 Python 代码解耦。  
> **现状 ✅**：`trigger_pattern` 匹配 · 多 skill 文件 · 基础注入逻辑。

- **[P1] [C:⭐ I:⭐⭐⭐] Skill 注入 Prompt**：`example_patch` / `suggested_tools` 写入 `[Skill 提示]`
- **[P1] [C:⭐ I:⭐⭐⭐⭐] priority + 最长 pattern 优先**：多 skill 命中时 deterministic 选最高 priority
- **[P2] [C:⭐ I:⭐⭐⭐] Skill 命中率 dashboard**：`matched_skill` + case_id 聚合进 eval report
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Skill 包目录**：`~/.fixloop/skills/` 覆盖

---

## 14. JSON 格式输出保证

> **设计边界**  
> Localizer / Patcher / Planner 产出 **结构化 JSON**；解析失败不能拖垮流水线。  
> **现状 ✅**：`parse_localizer_output` · `parse_patcher_output` · JSON extract from markdown fence。

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 多级 parse 降级**：strict JSON → json5 → regex extract `{...}` → 空结构 + error 进 feedback
- **[P1] [C:⭐ I:⭐⭐⭐⭐] schema 校验层**：Pydantic `SuspectList` / `PatchList` 校验字段类型与必填，失败附 `validation_errors[]` 重试
- **[P1] [C:⭐ I:⭐⭐⭐] 解析失败自动重试 prompt**：附「期望 schema 样例 + 你的输出错在何处」，最多 2 次 parse retry

---

## 15. 自愈闭环

> **设计边界**  
> 闭环 = **verify 失败 → 结构化 feedback → Patcher 重试 → 回滚/再 apply → 再 verify**，最多 `max_retries` 轮。  
> **现状 ✅**：`state.feedback` · patch 回滚 · `max_retries=3` · VerifyStrategy 抽象。

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 反馈环增强**：失败测试 + 上轮改动 + 回滚提示 + build_log → `state.feedback`
- **[P1] [C:⭐ I:⭐⭐⭐] feedback 滑动窗口**：最近 K 轮 verify 失败摘要 + 失败测试名集合
- **[P1] [C:⭐⭐ I:⭐⭐⭐] 终止条件枚举**：`fixed|exhausted|regression|timeout|user_cancel`，写入 `RepairState.status`

---

## 16. Docker 沙箱

> **设计边界（面试口径）**  
> 沙箱目标：**文件系统 · 网络 · 资源 · 权限**四维隔离。  
> **不防逻辑错误**：错误 patch 若仍通过 pytest，沙箱无法判定业务语义。  
> **有逃逸风险**：应文档化 threat model，**不声称「绝对安全」**。  
> **现状 ✅**：`network_mode=none` · `mem_limit`/`cpu_quota` · tar 传 `/code` · 单 Turn 结束 `destroy`。

### 16.1 文件系统隔离

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 仅暴露 `/code` + `/tmp` 可写**：只读 rootfs（`read_only=True` + tmpfs `/tmp`），禁止写 `/etc`、`/root`
- **[P1] [C:⭐ I:⭐⭐⭐] tar 排除与大小上限**：打包前排除 `.git`、`.venv`、`node_modules`；超 N MB 拒绝或白名单路径
- **[P2] [C:⭐ I:⭐⭐⭐] verify 后不留持久层**：`destroy` 必执行；温池 borrow 结束仍 reset 文件系统或换容器
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 宿主机零挂载**：禁止 bind mount 宿主机目录进容器；文档明确 Windows 亦同

### 16.2 网络隔离

- **[P1] [C:⭐ I:⭐⭐⭐⭐] ✅ 默认 `network_mode=none`**
- **[P1] [C:⭐ I:⭐⭐⭐] 网络策略文档**：README 说明「无网络 = 无法 runtime pip」

### 16.3 资源隔离

- **[P1] [C:⭐ I:⭐⭐⭐] ✅ mem_limit / cpu_quota**：已有 4g / 200000；写入 `fixloop.yaml` 可配
- **[P1] [C:⭐ I:⭐⭐⭐] sandbox 健康探针**：启动前 `docker info` + 镜像存在性 + `network_mode=none` 冒烟
- **[P2] [C:⭐ I:⭐⭐⭐] 全局并发沙箱上限**：`FIXLOOP_MAX_SANDBOXES` 信号量
- **[P2] [C:⭐ I:⭐⭐⭐] pytest 超时兜底**：`exit_code=-1` 仍生成明确 `failure_logs`

### 16.4 权限降级


- **[P2] [C:⭐ I:⭐⭐⭐] 禁止特权与 Docker-in-Docker**：`Privileged=false`、不挂载 `/var/run/docker.sock`
- **[P2] [C:⭐ I:⭐⭐⭐] 最小镜像 attack surface**：slim 基础镜像、固定 digest pin，CI 扫描 CVE

### 16.5 开销与逃逸回归


- **[P2] [C:⭐ I:⭐⭐⭐] 逃逸回归 Case**：`case_adv_sandbox_*` 尝试读 `/etc/passwd`、curl 外网、fork 爆炸


### 16.6 单 Turn 生命周期

- **[P1] [C:⭐ I:⭐⭐⭐] ✅ 单 Turn 生命周期**：`create` → 可选 `pip install` → `pytest --json-report` → `destroy`
- **[P1] [C:⭐⭐ I:⭐⭐⭐] cancel/timeout 统一 kill 路径**：`SandboxManager.execute` 超时或 cancel → `container.kill()` · 宿 workspace 回滚（[§12.7](#127-冲突--终止--恢复)）

---

## 17. Patch 与 Verify

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] AST 语义等价校验**：suspect 函数结构 diff，输出 `semantic_ok|drift`

---

## 18. 敏感信息处理

> **设计边界**  
> 三层：**L1 运行时隔离** · **L2 输出脱敏** · **L3 存储/索引排除**。  
> **现状 ✅**：Shell env 白名单 · `redact_text` / `redact_artifact` · trace 写入前 redact。

- **[P2] [C:⭐ I:⭐⭐⭐] prompt 注入 对抗 eval**：`case_adv_injection_*` 测 issue 中「忽略上文」类攻击
- **[P2] [C:⭐ I:⭐⭐⭐] trace 保留策略**：默认 30 天 TTL，租户可 wipe
- **[P2] [C:⭐ I:⭐⭐⭐] 敏感产物加密**：patch/issue 落盘可选 AES；租户 offboard 时 wipe 目录

---

## 19. 链路可观测

> **设计边界**  
> 每次 run 产出 **run_id + trace.jsonl + report.json**；L2 附加 **node_timings**。  
> **现状 ✅**：`_emit` 事件流 · Deterministic Replay · progress callbacks · TaskState 贯穿。

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 统一 run_id（UUID）**：L1 + L2 共用，防并发碰撞
- **[P1] [C:⭐ I:⭐⭐⭐] 结构化 JSON 日志**：`FIXLOOP_LOG=json`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] trace.jsonl gzip**：超 1000 行归档
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Prometheus `/metrics`**
- **[P2] [C:⭐ I:⭐⭐] Grafana dashboard JSON**：导入 node_timings + sandbox_ms 面板

---

## 20. 消融实验与评测

> **设计边界**  
> 消融 = 同一 Case 集上切换 **full / single / no_retriever** 等变体。  
> **现状 ✅**：10 Case · 60 runs 正式结果 · `variants.py` · `regression_check`。

### 20.1 Case 库

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Case 011–020**：按错误类型矩阵扩展
- **[P1] [C:⭐ I:⭐⭐⭐⭐] 难度重标定**：依据 60 runs 上调或标 `requires_retriever`
- **[P2] [C:⭐ I:⭐⭐⭐] 负样本 Case**：ambiguous issue，期望 `exhausted`
- **[P3] [C:⭐⭐ I:⭐⭐] 多语言 Case**：Node + `language: java`

### 20.2 Runner 与指标

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] patch_equivalence_score**：actual vs expected → `full|partial|none`
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 并行跑 Case**：`run_all(workers=N)` + temp 隔离 + API 限流
- **[P1] [C:⭐ I:⭐⭐⭐⭐] `--resume` 断点续跑**：跳过已完成 `(variant, case, rep)`
- **[P2] [C:⭐ I:⭐⭐⭐] Pass@k**：同 Case 跑 k 次，报告 pass@1 / pass@3
- **[P2] [C:⭐ I:⭐⭐⭐] 分 Agent token / latency 表**：`by_agent` 进 `final_report.md`

---

## 21. CLI · REPL · API

- **[P1] [C:⭐ I:⭐⭐⭐] 命令历史**：`readline` + Ctrl-R
- **[P1] [C:⭐ I:⭐⭐⭐] `/memory` / `/memory forget`**：见 [§4.7](#47-运维与面试要点)
- **[P2] [C:⭐ I:⭐⭐] 多行输入**：`\` 续行
- **[P2] [C:⭐⭐ I:⭐⭐⭐] /save /load /sessions /replay /prompt**：会话迁移、run 列表、trace 回放、prompt 调试
- **[P2] [C:⭐⭐ I:⭐⭐⭐⭐] REST API**：`--serve :8000`，POST `/ask` + GET `/session/{id}`
- **[P2] [C:⭐ I:⭐⭐⭐] ✅ `--health` / `--profile` 增强**：health 增 provider ping；profile 文档化 dev/prod/ci
- **[P1] [C:⭐ I:⭐⭐⭐⭐] repair 退出码**：0 成功 / 1 失败 / 2 配置 / 3 超时

---

## 22. 配置 · 插件 · 可靠性

- **[P2] [C:⭐ I:⭐⭐⭐] 增量 repo snapshot**：仅 hash 变更文件

---

## 23. Web 产品化与多用户

> **设计边界**  
> **MVP 范围**：优先 **L2 repair Web 化**；L1 Agent REPL 可二期。  
> **沙箱不变**：Web 层只调 Orchestrator + Harness，**不 bypass** ToolGateway。  
> **现状**：L1 仅有 `--serve` 雏形；**无** Web UI、**无** 认证、**无** 多租户存储。

### 23.1 前端

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 实时进度页**：SSE 订阅 localize → retrieve → patch → verify
- **[P1] [C:⭐⭐ I:⭐⭐⭐] 结果页**：patch diff 高亮、verify 报告、下载 `.patch`

### 23.2 HTTP API

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] REST v1 契约**：`POST /api/v1/repairs` · `GET .../{id}` · `POST .../cancel`
- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] SSE**：`GET /api/v1/repairs/{id}/events`
- **[P2] [C:⭐ I:⭐⭐⭐] Idempotency-Key**：重复 POST 同 key 返回同一 `repair_id`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] WebSocket 备选**：双向 cancel + 心跳
- **[P2] [C:⭐ I:⭐⭐⭐] 健康检查**：`/health` liveness · `/ready` 检查 Redis + Docker

### 23.3 认证 · 配额 · 隔离

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 用户身份 + 租户隔离**：JWT/session · `tenant_id`/`user_id` · 路径 `.agent/tenants/{tenant}/users/{user}/`
- **[P1] [C:⭐⭐ I:⭐⭐⭐] 每用户 API Key + 速率/配额**：`Bearer fl_...` · RPS · 并发 repair · 日 token 预算；超配额 429 + audit
- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] workspace jail**：每次 repair 独立 temp 目录，结束销毁
- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 同 repo 写锁**：同一 `repo_id` 只允许一个 inflight repair 写 workspace
- **[P1] [C:⭐⭐ I:⭐⭐⭐] Docker 槽位 per 租户**：`max_concurrent_sandboxes`
- **[P2] [C:⭐ I:⭐⭐⭐] CSRF / 安全头**：SameSite + CSRF token；CSP 防 XSS
- **[P2] [C:⭐ I:⭐⭐] 审计日志**：谁、何时、对哪 repo 发起了 repair

### 23.4 Worker · 并发


- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 公平队列 + Worker 扩展**：weighted fair queue · K8s HPA 按队列深度
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 任务队列**：Redis/RQ，Worker 异步执行
- **[P2] [C:⭐ I:⭐⭐⭐] 死信与重试**：失败 N 次进 DLQ

### 23.5 部署 · Web 安全

- **[P2] [C:⭐⭐ I:⭐⭐⭐] 多实例 trace 存储**：NFS / S3 挂载 `.agent`

---

## 24. 压测与容量

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 压测场景库 + Locust/k6 驱动**

---

## 25. 演示 · 文档 · 测试

- **[P1] [C:⭐ I:⭐⭐⭐] CLI 退出码单测**
- **[P2] [C:⭐ I:⭐⭐] Skill 匹配 / Skill 命中单测**

---

*筛选版 · base `master` @ PR #85 · 476 tests · 80% coverage*
