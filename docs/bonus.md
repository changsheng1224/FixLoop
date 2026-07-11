# FixLoop Bonus 待实现条目

> **仅 backlog**；设计思路见 [docs/bonus/DESIGN.md](bonus/DESIGN.md) · 产品边界与 Web 归档见 [OUT_OF_SCOPE.md](bonus/OUT_OF_SCOPE.md)。  
> **产品边界**：本地 CLI / REPL + `src.cli repair`；不实现 Web / HTTP / 多租户。  
> 基线：`master` @ PR #93 · **755+ tests**。格式：**[P?] [C:复杂度 I:面试价值] 标题**：方案摘要。  
> **完成标记**：`✅` 已完成 · `🔶` 部分完成（条目均保留）。本分支 `V1.1-Bonus6-Context工程` 见 §3。  
> **排序**：章内 **P1 → P2 → P3**；同优先级按 **依赖先后**（schema/主路径 → 观测/增强）。小节号与 [DESIGN](bonus/DESIGN.md) 对齐时，**阅读顺序**可为先 P1 块（如 §6.2 grep、§12.2–12.8、§13.5 在 §13.4 前）。

---

## 目录

| Bonus § | 设计（DESIGN） | 说明 |
|---------|----------------|------|
| [1](#1-agent-运行时)–[21](#21-cli--repl本地) | [§1–§21](bonus/DESIGN.md) | 运行时 → 评测主路径 |
| [22](#22-意图识别与路由) | [§23 意图识别](bonus/DESIGN.md#23-意图识别与路由) | bonus 章号与 DESIGN 错开 |
| [23](#23-演示--文档--测试) | [§25 演示/测试](bonus/DESIGN.md#25-演示--文档--测试) | |
| [24](#24-输出质量--幻觉探针--judge-eval) | [§26 输出质量](bonus/DESIGN.md#26-输出质量--幻觉探针--judge-eval) | |
| — | [OUT_OF_SCOPE](bonus/OUT_OF_SCOPE.md) | 🚫 不实现 |

---

## 1. Agent 运行时

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 统一 logger + `--log-level`**
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 统一 token 会计**：session 级汇总进 `report.json`（字段规范见 [§19.4](#194-核心运行时指标监控)）
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Agent 池化 / 预热**：repair 启动预建 Localizer/Retriever 实例，复用 prefix hash 与 memory 投影，降首轮 latency
- **[P2] [C:⭐⭐ I:⭐⭐⭐] REPL 热重载**：`/config max_steps=10`
- **[P2] [C:⭐ I:⭐⭐⭐] workspace 切换检测**：`cwd` 变更时 invalidate prefix hash + working memory recent_files

---

## 2. Agent Loop / ReAct

### 2.1 用户中断与取消

- **[P1] ✅ [C:⭐⭐⭐ I:⭐⭐⭐⭐] CancellationToken 全链路**：AgentLoop · ModelClient · ToolExecutor · Patcher `complete_once` · sandbox verify 共享；**Ctrl+C** · REPL **`/cancel`** 置位
- **[P1] ✅ [C:⭐⭐ I:⭐⭐⭐⭐] 协作式 cancel 检查点**：每 step 开始前 · model 返回后 · **每次 `execute_tool` 前/后**检查；已 cancel 则不再调度新 tool
- **[P1] ✅ [C:⭐⭐ I:⭐⭐⭐] TaskState.user_cancel**：`status=stopped` · `stop_reason=user_cancel` · trace 含 phase + in-flight tool
- **[P1] ✅ [C:⭐ I:⭐⭐⭐] cancel 后 workspace 一致性**：write/patch/shell 依赖 Gate 8/9 snapshot diff + restore；L2 verify cancel → container kill + repo restore（[§8](bonus/DESIGN.md#8-工具安全闸口)）
- **[P2] 🔶 [C:⭐⭐ I:⭐⭐⭐] 流式模型 cancel**：chunk 循环内检查 token，立即 abort 并关闭连接（Ollama `complete_stream` ✅；默认 ask 未接 streaming）
- **[P2] ✅ [C:⭐ I:⭐⭐⭐] REPL `/cancel` 或二次 Ctrl+C**：向当前 `AgentLoop` 实例下发 cancel，不杀整个进程

### 2.2 执行前 Plan · TodoList

> 设计见 [DESIGN §2.2](bonus/DESIGN.md#22-执行前-plan--todolist)。L2 子任务见 [§12.8](#128-子问题拆分)。

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] Plan 阶段 + TodoList schema**：ReAct 前 light_client 输出 `[{id, content, status}]`；写入 `session["plan_todos"]` · trace `plan_created`
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Todo 状态机 + trace**：`pending|in_progress|done|cancelled`；每 step `todo_updated`；与 [§3.1 state 段](#31-设计原则) 投影
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 空转 / replan 读 todo 进度**：连续 N 步无进展 → 读 [§2.3](#23-loop-engineering--middleware--防死循环) stall 信号 · mark blocked
- **[P2] [C:⭐ I:⭐⭐⭐] REPL `/todos`**：列出当前 plan · mark done；repair 模式只读展示 Orchestrator phase todo

### 2.3 Loop Engineering · Middleware · 防死循环

> 设计见 [DESIGN §2.3](bonus/DESIGN.md#23-loop-engineering--middleware--防死循环)。模块：`agent_loop.py` · `callbacks.py` · `tool_executor.py`。

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Middleware 链 + Callback 全覆盖**：扩展 `AgentCallback`（`pre_model` / `post_model` / `pre_tool` / `post_tool` · `on_step_start` / `on_final_answer`）；native 与 XML 路径统一 invoke
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 死循环检测**：相同 `(tool_name, args_hash)` 连续 **K** 次 → `stop_reason=circuit_breaker` · trace `loop_detected`
- **[P1] [C:⭐ I:⭐⭐⭐⭐] 目标漂移 / stall 终止**：连续 N 步无 `affected_paths` 且无 final → `stop_reason=stall` · task_summary 锚定 · 提示 replan
- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 显式 ReAct 阶段 trace** ✅：每步 `react_phase: reasoning|acting|observation|recording`（native/XML 双路径）
- **[P2] 🔶 [C:⭐ I:⭐⭐⭐] Agentic Loop trace 事件表** ✅：`loop_trace_schema.py` 五段映射 + XML/Native 快照单测；Native 补齐 `context_built`

### 2.4 ReAct 步进 · 超时 · 解析

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 单步工具超时** ✅：`tool_timeout.py` + Gate 9 `concurrent.futures`（默认 120s，`0`=禁用）
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 单步 wall-clock 超时** ✅：`step_clock.py` + 边界检查，`stop_reason=step_timeout`（默认 300s）
- **[P2] [C:⭐ I:⭐⭐⭐] stop_reason 枚举** ✅：`stop_reasons.py` `StopReason` + legacy 归一化 + `stop_reason_detail`
- **[P2] [C:⭐ I:⭐⭐⭐] 解析失败 recovery prompt** ✅：`parse_recovery.py` 片段 + caret + `parse_retry` trace
- **[P2] [C:⭐ I:⭐⭐] final_answer schema 校验**：可选 JSON mode final（如 repair 子任务），失败则回到 Acting
- **[P3] [C:⭐⭐ I:⭐⭐⭐] CoT 提取**：thinking 块剥离后再进 history

---

## 3. Context 工程

### 3.1 设计原则

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] history canonical JSONL**：`.agent/history.jsonl` 只追加；`build()` 只读投影
- **[P1] ✅ [C:⭐⭐⭐ I:⭐⭐⭐⭐] 八段 Context 投影 schema**：`metadata.context_sections` 八段语义键 + legacy `sections` 双写；`context_built` trace（`3c67e15`）
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] state section 注入**：`plan_todos` · `TaskState.phase` · repair 摘要进 build（交叉 [§2.2](#22-执行前-plan--todolist)）— `plan_todos` 仅计数进 `context_sections.state`，未注入 prompt
- **[P2] [C:⭐⭐ I:⭐⭐⭐] knowledge 与 memory 分工**：`relevant` 重命名为 knowledge 语义；working 留 memory section
- **[P2] ✅ [C:⭐ I:⭐⭐⭐] system/tools/skills 拆 prefix**：`PromptPrefix` 三段 + 独立 budget + `hash(system+tools)`（`e7bb398`）

### 3.2 五 Section 组装与 Token 预算

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 五 Section 独立硬顶 enforce**：`add_section` 超 `BUDGET_*` 即 fit，不单靠 TOTAL 双限（DESIGN Gap）
- **[P1] ✅ [C:⭐ I:⭐⭐⭐⭐] fit 裁剪不 splice prefix**：system/tools/skills `add_stable_section` 整段丢弃（`e7bb398`）
- **[P1] ✅ [C:⭐ I:⭐⭐⭐] Tools 仅注入启用集**：`tool_names` L0 过滤 + 独立 **tools** 段（`e7bb398`）
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Skills 索引 + 全文按需**：prefix/catalog 仅索引；命中后注入 **skills** 段（见 [§13.4](#134-海量-skill-加载)）
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] tier_pins.yaml 接线 L0 + L2**：`orchestrator_pin_fields` 与 compression L0 共读 yaml（与 [§3.4](#34-l2-repair-与-memory-衔接) 联动）
- **[P1] ✅ [C:⭐ I:⭐⭐⭐] 多模型 tokenizer 切换**：`tokenizer_registry` + fallback warn + L2 `fit_repair_user_prompt`（`5b7ea90`）
- **[P2] ✅ [C:⭐ I:⭐⭐⭐] prefix 分段 hash**：`prefix_hashes` 观测字段（system/tools/skills/cache_key + 指纹）进 metadata 与 `context_built` trace
- **[P2] ✅ [C:⭐⭐ I:⭐⭐⭐] User Message 模板化**：stdlib Template + `.agent/task_template.md` / `src/prompts/tasks/*.md`；L1 metadata + L2 repair user 模板
- **[P2] [C:⭐ I:⭐⭐⭐] fit 保护优先级单测矩阵**：request > prefix > memory > relevant > history 回归

### 3.3 压缩管线 L0–L5

- **[P1] ✅ [C:⭐ I:⭐⭐⭐⭐] L5 摘要不污染 prefix hash**：`prompt_cache_key` = `hash(system+tools)`；skills/examples 变更不 bust（`e7bb398`）
- **[P1] [C:⭐ I:⭐⭐⭐] native 路径接入全管线**：`chat_with_native_tools` history 走 L0–L5
- **[P1] [C:⭐ I:⭐⭐⭐] 摘要缓存持久化**：`_summary_cache` 落盘 `.agent/summary_cache/`（现状内存 dict）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 增量摘要**：在 `[Earlier summary]` 上追加，避免每轮全量重摘要

### 3.4 L2 Repair 与 Memory 衔接

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 钉扎区 enforce**：issue/stack/suspect.file_path 永不裁剪；L5 摘要不得覆盖（`tier_pins.yaml` + [§3.5](#35-上下文传递--压缩触发--user-保护) 单测）
- **[P1] 🔶 [C:⭐⭐ I:⭐⭐⭐⭐] 共享 TokenBudget 库**：L2 patcher `fit_repair_user_prompt` + `tokenizer_by_agent`；localizer/retriever 经 `ask()`→CM（`5b7ea90`）
- **[P2] [C:⭐ I:⭐⭐⭐] 分 Agent 预算表**：Localizer 2k / Retriever 3k / Patcher 4k / Verifier 1k

### 3.5 上下文传递 · 压缩触发 · user 保护

> 设计见 [DESIGN §3.5](bonus/DESIGN.md#35-上下文传递--压缩触发--user-保护)。

- **[P1] ✅ [C:⭐ I:⭐⭐⭐⭐] user message 永不压缩 enforce**：reserve-first task 预留 + `fit_repair` 只裁 system；`request_preserved` metadata
- **[P1] [C:⭐ I:⭐⭐⭐⭐] 压缩触发 trace**：L2/L5 超阈值时 emit `compression_triggered{level,ratio,section}`；与 [§19.4](#194-核心运行时指标监控) 同源

---

## 4. 分层记忆

### 4.1 设计原则

> 设计见 [DESIGN §4.1](bonus/DESIGN.md#41-设计原则)。本章无独立 backlog 条目。

### 4.2 四层模型与数据流

- **[P2] [C:⭐ I:⭐⭐⭐] recent_files 显式 LRU + last_access**：超 `MAX_RECENT_FILES` 淘汰最旧
- **[P2] [C:⭐ I:⭐⭐⭐] episodic kind 分类检索**：error/decision/observation 分权重
- **[P2] [C:⭐⭐ I:⭐⭐⭐] episodic → durable 晋升**：`kind=decision` 且多次被检索 → 自动 promote

### 4.3 写入管线

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Candidate schema + 规则/LLM 双路抽取**：LLM 仅填规划 topic/key，禁止自由建库
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 冲突状态机**：`None|Equivalent|Override|Invalid` + 权威序；低权威不覆盖高权威
- **[P2] [C:⭐ I:⭐⭐] 互斥 key 版本链**：同 topic 语义互斥（如 Python 版本）保留 history 链而非覆盖

### 4.4 召回与 Context 投影

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] embed_query 与 user request 分离**：`derive_embed_query()`（`task_summary` → 规则抽取 → head/tail）；query 意图见 [§22.4](#224-l1-会话意图repl--memory)
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] EMBED_MAX_TOKENS + head/tail 截断**：按模型 `max_seq_length`；stack 保留 **Traceback 尾段 + 最后一帧 File/line**
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 语料 chunk + max-pool 检索**：durable/precedent 超 `EMBED_MAX_TOKENS` 按段 embed，query 与任一段 max cosine
- **[P2] [C:⭐ I:⭐⭐⭐] embedding 磁盘缓存**：normalize 后 content_hash → `.agent/embed_cache/`

### 4.5 质量 · 衰减 · 隔离

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 本地路径隔离**：禁止 path 遍历读 workspace 外 `.agent/memory/`
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Memory Dream 后台任务**：idle / repair 结束；去重 · 过期 · index 重建
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 记忆 GC + episodic 上限**：durable LRU；episodic 超 `MAX_EPISODIC_NOTES` 淘汰
- **[P2] [C:⭐ I:⭐⭐⭐] 置信度时间衰减**：`confidence *= decay^(days_since_seen)`，低于阈值不参与召回
- **[P2] [C:⭐ I:⭐⭐⭐] 健康 metric 进 report**：条目数、重复率、平均 confidence、Dream 时间

### 4.6 L2 Repair 记忆桥接

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] repair precedent 读写一体**：启动读 topics → `similar_fixes`；`status=fixed` → upsert（issue 类型 + patch 摘要 + case_id）
- **[P1] [C:⭐ I:⭐⭐⭐] similar_fixes 置信度闸口**：semantic score < threshold 不注入 Patcher
- **[P2] [C:⭐ I:⭐⭐] 不信任记忆覆盖 suspect**：先例仅 hint，Localizer 仍走 stack/AST


### 4.7 用户画像 · Embedding 迁移

> 设计见 [DESIGN §4.8](bonus/DESIGN.md#48-用户画像--遗忘--embedding-迁移)。

- **[P2] [C:⭐⭐ I:⭐⭐⭐⭐] 结构化用户画像 schema**：durable `preferences` · pydantic `key/value/confidence/source`

---

## 5. Prompt

> 设计见 [DESIGN §5](bonus/DESIGN.md#5-prompt)。issue_type 路由见 [§22.3](#223-意图--编排路由)。

### 5.1 Prefix · Cache · Rules

> **提高命中率**：system+tools 稳定 · history 只追加（[§3.1](#31-设计原则)）· 动态字段不进 prefix；指标见 [§19.4](#194-核心运行时指标监控)。

- **[P1] [C:⭐ I:⭐⭐⭐⭐] prefix 禁动态字段**：system/rules 禁止 `timestamp` · `run_id` · `session_id` · random nonce
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 可缓存段前置 · 动态段后置**：system → tools → skills(索引) → … → history
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 工具 schema 集稳定化**：`_tool_names` **字典序固定**；repair 全流程同一 tool 集
- **[P2] [C:⭐ I:⭐⭐⭐] workspace fingerprint 降噪**：git dirty 用 **file-set content hash** 而非 mtime/全文 diff
- **[P2] [C:⭐⭐ I:⭐⭐⭐] repair 多 phase 共享 L1 prefix**：Orchestrator 复用同一 `prompt_cache_key`；L2 角色 system **不参与** L1 hash
- **[P2] [C:⭐ I:⭐⭐⭐] 多轮 messages 前缀对齐**：每 step 全量 history 投影；禁止 divergent message 序列
- **[P2] [C:⭐⭐ I:⭐⭐⭐] few-shot / rules 外置**：`.agent/examples.md`、`.agent/rules.md`；变更 invalidate prefix hash

### 5.3 模板 · Skill 块

- **[P1] [C:⭐ I:⭐⭐⭐] Skill 块注入 Prompt 统一**：`[Skill 提示]` = `suggested_tools` + `example_patch`（见 [§13.3](#133-注入)）

### 5.2 L2 角色 Prompt

- **[P2] [C:⭐ I:⭐⭐⭐] 分 issue 类型 prompt 变体**：ImportError 与 logic_error 不同 patcher 后缀（自动选择见 [§22.3](#223-意图--编排路由)）

---

## 6. Agent Tool

> 设计见 [DESIGN §6](bonus/DESIGN.md#6-agent-tool)。权限见 [§7](#7-toolgateway)。

### 6.1 L1 通用工具

- **[P1] [C:⭐ I:⭐⭐⭐] write_file 原子写**：先写 `.tmp` 再 `replace`
- **[P1] [C:⭐ I:⭐⭐⭐] patch_file 多 hunk 预览**：apply 前 diff 摘要进 trace/审批（[§8](bonus/DESIGN.md#8-工具安全闸口)）
- **[P2] [C:⭐ I:⭐⭐] list_files glob / depth**：`pattern="*.py"`、`depth=1` 限制递归（列路径；内容搜索用 `grep`）
- **[P2] [C:⭐ I:⭐⭐⭐] run_shell 环境变量白名单**：与 [§18](bonus/DESIGN.md#18-敏感信息处理) 联动

### 6.2 Grep 工具（rg 封装）

> 设计见 [DESIGN §6.5](bonus/DESIGN.md#65-grep-工具rg-封装)。模块：`agent_runtime/tools.py` · `ToolGateway` · `src/tools/registry.py`（L2 可见性）。  
> **定位**：与 Claude Code **Grep** 对齐 — 只读内容搜索 · **禁止** `run_shell rg`；底层复用现有 `_search_rg` / `IGNORED_PATH_NAMES`。

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] `grep` 工具注册 + GrepArgs schema**：`pattern`（必填）· `path`（默认 `.`）· `glob`（如 `*.py`）· `ignore_case` · `context_lines` · `max_results`；`auto_schema` + `auto_validate`
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] `tool_grep` 执行（rg 优先）**：`rg -n --smart-case` + `--glob` + `-C`；不可用时 Python `re` + `rglob` fallback；结果格式 `path:line:text`
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Gateway + 配额接线**：`grep` 与 `read_file`/`search` 同属读类；Localizer/Retriever `allow` · Patcher 默认 `deny`；占 `total_tools` quota
- **[P1] [C:⭐ I:⭐⭐⭐⭐] L1/L2 Agent 工具集接入**：`build_default_tools` / factory 注册 `grep`；`.agent/tools.yaml` manifest 与 [§6.4](#64-schema-工程--manifest) checklist 同步
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Retriever 路径改为 stack→read→grep**：Orchestrator / `--fast-retrieve` 规则检索走 `grep`；trace `retrieval_path=rule`（交叉 [§10.2](#102-修复流水线降级)）
- **[P2] [C:⭐ I:⭐⭐⭐] search 转调 grep（兼容）**：保留 `search` 工具名；内部委托 `tool_grep` · prompt 引导新调用用 `grep`
- **[P2] [C:⭐ I:⭐⭐⭐] grep 结果截断与去重**：超 `max_results` 附 `...N more`；同文件相邻行合并展示

### 6.3 注册与 L2 领域工具

- **[P2] [C:⭐ I:⭐⭐⭐] 工具组合 ToolGroup**：`inspect_file` = read_file + ast_parse，占 1 次 quota
- **[P2] [C:⭐ I:⭐⭐⭐] Localizer 工具顺序**：stack_parse → ast_parse；违规 warn
- **[P2] [C:⭐ I:⭐⭐⭐] ast_parse 局部解析**：仅 suspect 行附近 AST

### 6.4 Schema 工程 · Manifest

- **[P2] [C:⭐⭐ I:⭐⭐⭐⭐] `.agent/tools.yaml` manifest**：按 Agent 可见性 merge；与 [§7](#7-toolgateway) 权限表校验
- **[P2] [C:⭐ I:⭐⭐⭐] L2 registry 与 auto_schema 一致性**：`src/tools/registry.py` 字段变更 CI 告警

---

## 7. ToolGateway

> 设计见 [DESIGN §7](bonus/DESIGN.md#7-toolgateway)。Executor 闸口见 [§8](#8-工具安全闸口)。

### 7.1 Function Calling 执行环

> 设计见 [DESIGN §7.4](bonus/DESIGN.md#74-function-calling-执行环)。

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 模型只见 schema · 执行仅经 Gateway**：`execute_tool` 仅 `ToolGateway.dispatch`；单测无 bypass
- **[P2] [C:⭐ I:⭐⭐⭐] 危险工具双层闸**：Gateway deny + Executor Gate 审批（write/shell）；[§8](#8-工具安全闸口) · [§16](#16-docker-沙箱)

### 7.2 调度 · 审计

- **[P2] [C:⭐ I:⭐⭐⭐] ToolGateway 越权审计**：`permission_denied` → trace / agent_errors / [§19.3](#193-工具--gateway-指标)
- **[P2] [C:⭐ I:⭐⭐⭐] 双层拒绝语义**：Gateway=角色不允许；Executor=参数/配额不允许


---

## 8. 工具安全闸口

- **[P1] [C:⭐ I:⭐⭐⭐] 审批时 diff 预览**：write_file / patch_file 审批时显示 patch 前后片段
- **[P1] [C:⭐ I:⭐⭐⭐] Gate 5 语义 duplicate**：同 tool + 同 path 即使 text 不同也视为重复 read
- **[P2] [C:⭐ I:⭐⭐⭐] Gate 7 分级审批**：write/patch 需 ask，read/search/grep auto
- **[P2] [C:⭐ I:⭐⭐⭐] 符号链接逃逸检测** ✅：`path_safety.resolve_under_root` + `ToolContext.resolve` 分量遍历 + symlink 校验
- **[P2] [C:⭐ I:⭐⭐⭐] 闸口拒绝统计** ✅：Gate 3 路径预检 + `tool_rejections_by_gate` / `tool_rejection_metrics` 进 report 与 trace（Grafana）

---

## 9. 硬上限与工具配额

- **[P1] [C:⭐ I:⭐⭐⭐] 分 Agent 配额**：Patcher write 与 Localizer read 分开计数
- **[P2] [C:⭐ I:⭐⭐⭐] context token 硬顶**：`HARD_CAP=8000` 仍超则拒绝 ask（[§3.2](#32-五-section-组装与-token-预算)）

---

## 10. 限流 · 熔断 · 降级

### 10.1 模型 API

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐] Ollama / OpenAI streaming**：SSE/chunk 增量解析，REPL 实时输出
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 熔断事件进 trace**：`circuit_opened` / `half_open_probe` / `circuit_closed`
- **[P2] ✅ [C:⭐ I:⭐⭐⭐] Retry-After + jitter**：429 退避加随机抖动（`retry_policy` + Anthropic client）
- **[P2] ✅ [C:⭐ I:⭐⭐⭐] 半开成功阈值**：连续 2 次 probe 成功才 CLOSED（`half_open_success_threshold`）
- **[P3] [C:⭐⭐ I:⭐⭐] HTTP keep-alive**：同 session 连接复用

### 10.2 修复流水线降级

- **[P1] [C:⭐ I:⭐⭐⭐] Retriever 降级规则检索**：LLM 超时 → 堆栈文件名 + **grep**；补 `related_tests`（路径见 [§6.2](#62-grep-工具rg-封装)）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Multi-Agent 降级 Single-Agent**：verify 连续失败后 `degraded_mode` + baseline

---

## 11. Checkpoint 断点恢复与续跑

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 每 tool 步 checkpoint**：`trigger=step_end`；`--resume` 从最后成功步继续
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] L2 阶段 checkpoint**：`repair_checkpoint.json` · `--resume-repair <run_id>`
- **[P1] [C:⭐⭐ I:⭐⭐⭐] cancel 时写 checkpoint**：`trigger=user_cancel` · 含最后成功 tool step + `in_flight_tool`
- **[P2] [C:⭐ I:⭐⭐] SessionStore 损坏恢复**：`.bak` 或跳过告警

---

## 12. Multi-Agent 编排

### 12.1 流水线编排

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 分阶段超时**：localize / patcher / verify 独立 timeout

### 12.2 State 三层模型

- **[P1] [C:⭐ I:⭐⭐⭐] 状态机显式枚举**：`phase: localize|retrieve|patch|verify|done|failed` 与 `status` 终态分离（[§15](#15-自愈闭环)）
- **[P2] [C:⭐ I:⭐⭐⭐] repair 落盘**：`.agent/repairs/{id}/repair_state.json` + timings
- **[P2] [C:⭐ I:⭐⭐⭐] L1/L2 State 关联字段**：`RepairState.run_id` ↔ 各 Agent `TaskState.run_id` 进 trace

### 12.3 Blackboard 与 Agent 通信

> 设计见 [DESIGN §12.5](bonus/DESIGN.md#125-blackboard-与-agent-通信)。

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] Blackboard 接入 Orchestrator 主路径**：Localizer/Retriever write `suspect:*` / `context:*`
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] merge 阶段读 Blackboard**：Patcher 用 `read_related("suspect/")` + `RepairState`；冲突走 [§12.7](#127-冲突--终止--恢复)
- **[P1] [C:⭐ I:⭐⭐⭐⭐] Blackboard snapshot 进 trace/checkpoint**：`entries` · `conflicts[]` 可 resume（[§11](#11-checkpoint-断点恢复与续跑)）
- **[P2] [C:⭐ I:⭐⭐] 前缀订阅**：`read_related("suspect/")` 批量注入，替代手工拼块

### 12.4 并发与一致性

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 阶段级读写锁**：localize/retrieve 共享读；Patcher 独占写
- **[P1] [C:⭐ I:⭐⭐⭐] workspace 写窗口单飞**：同一时刻最多一个 patch phase
- **[P2] [C:⭐ I:⭐⭐⭐] 分 Agent 独立 session/quota**：并行 Agent 不共享 L1 session
- **[P2] [C:⭐ I:⭐⭐] concurrent tool 硬顶**：并行 subprocess 上限（[§9](#9-硬上限与工具配额)）

### 12.5 子问题拆分

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] RepairPlan.subtasks schema**：`[{id, goal, suspect_files, depends_on}]`
- **[P2] [C:⭐ I:⭐⭐⭐] composite Case 驱动**：eval composite 验证拆分+合并

### 12.6 冲突 · 终止 · 恢复

- **[P1] [C:⭐⭐ I:⭐⭐⭐] Orchestrator 冲突仲裁 API**：`resolve_conflict(key, strategy=...)`
- **[P2] [C:⭐ I:⭐⭐⭐] Localizer∥Retriever 去重**：同 file_path + 行号合并
- **[P2] [C:⭐ I:⭐⭐] 冲突进 trace/report**：`blackboard_conflicts[]`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 降级 Single-Agent 最后一搏**：见 [§10.2](#102-修复流水线降级)


### 12.7 角色与机制

- **[P2] [C:⭐⭐ I:⭐⭐⭐] 动态 Agent 裁剪**：简单 import 跳过 Retriever；composite 强制四 Agent（[§22.3](#223-意图--编排路由)）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Planner Agent**：输出 `RepairPlan` JSON，Orchestrator 按 plan 调度

### 12.8 子 Agent 职责 · 共享工具

> 设计见 [DESIGN §12.9](bonus/DESIGN.md#129-子-agent-职责边界--共享工具)。

- **[P2] [C:⭐ I:⭐⭐⭐] 子 Agent 共享 ToolGateway 实例**：同一 workspace · 分 Agent quota/session

### 12.9 Reviewer–Executor 防震荡

> 设计见 [DESIGN §12.10](bonus/DESIGN.md#1210-reviewerexecutor-防震荡--parse-状态-reconcile)。

- **[P2] [C:⭐⭐ I:⭐⭐⭐] verify 失败冷却轮**：连续相同 failure 摘要 → 降 temperature 或 skip Retriever · 防震荡

---

## 13. Skill

> 设计见 [DESIGN §13](bonus/DESIGN.md#13-skill)。匹配见 [§22.2](#222-skill-策略匹配)。

### 13.1 YAML Schema

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Skill YAML schema 校验**：pydantic 校验 `name` · `language` · `trigger_pattern` · `priority`

### 13.2 匹配算法

- **[P1] [C:⭐ I:⭐⭐⭐⭐] priority + 最长 pattern 优先**：多 skill 命中时 deterministic 选最高 priority

### 13.3 注入

- **[P1] [C:⭐ I:⭐⭐⭐] Skill 注入 Prompt**：`example_patch` / `suggested_tools` → `[Skill 提示]`（[§5.3](#53-模板--skill-块)）

### 13.5 Skill 召回率 · 版本

> 设计见 [DESIGN §13.5](bonus/DESIGN.md#135-skill-召回率--版本--质量-rubric)。

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Skill 召回率 eval**：`matched_skill` × `case_id` → precision/recall · `eval_report.skill_metrics`（[§20.3](#203-agent-性能量化--judge--检索质量)）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Skill content_hash + 原子 swap**：索引 rebuild 写 temp → rename

### 13.6 Skill 与 tool 联动

- **[P1] [C:⭐ I:⭐⭐⭐⭐] matched skill → suggested_tools 约束 Gateway**：未在白名单的工具 warn/deny（可选 strict）
- **[P2] [C:⭐ I:⭐⭐⭐] skill 未命中 default 策略**：trace `matched_skill=null` · generic patcher 后缀

### 13.4 海量 Skill 加载

> 设计见 [DESIGN §13.4](bonus/DESIGN.md#134-海量-skill-加载)。

- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐⭐] Skill 向量检索（N>100）**：embed 描述 · query=issue/task_summary · top-k 再 regex 确认

---

## 14. JSON 格式输出保证

> 设计见 [DESIGN §14](bonus/DESIGN.md#14-json-格式输出保证)。

### 14.1 结构化输出全栈

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 多级 parse 降级**：strict JSON → json5 → regex extract `{...}` → 空结构 + error 进 feedback
- **[P1] [C:⭐ I:⭐⭐⭐⭐] schema 校验层**：Pydantic `SuspectList` / `PatchList`；失败附 `validation_errors[]` 重试
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Provider native JSON mode**：`response_format` / JSON mode 与 XML 解析并存 · per-agent 配置
- **[P1] [C:⭐ I:⭐⭐⭐] 解析失败自动重试 prompt**：附 schema 样例 + 错因，最多 2 次 parse retry

---

## 15. 自愈闭环

> 设计见 [DESIGN §15](bonus/DESIGN.md#15-自愈闭环)。

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 反馈环增强**：失败测试 + 上轮改动 + 回滚提示 + build_log → `state.feedback`
- **[P1] [C:⭐⭐ I:⭐⭐⭐] 终止条件枚举** ✅：`fixed|exhausted|regression|timeout|user_cancel` → `RepairState.status`

### 15.1 Bad Case 闭环

> 设计见 [DESIGN §15.1](bonus/DESIGN.md#151-bad-case-采集--回归闭环)。

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] badcase → 新 eval Case 晋升**：标注 `expected_patch` · 进 `src/eval/cases/` · CI 回归
- **[P2] [C:⭐ I:⭐⭐⭐] 失败分类 tag** ✅：`parse_fail|wrong_file|regression|timeout` 进 badcase metadata

---

## 16. Docker 沙箱

### 16.1 文件系统隔离

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 仅暴露 `/code` + `/tmp` 可写** ✅：`read_only=True` + tmpfs `/code`/`/tmp`；pip `--user` 写入 `/code/.local`
- **[P1] [C:⭐ I:⭐⭐⭐] tar 排除与大小上限** ✅：``sandbox_tar`` 预检打包；排除 `.git`/`.venv`/`node_modules` 等；默认 200MB 拒绝（`FIXLOOP_SANDBOX_TAR_MAX_MB`）
- **[P2] [C:⭐ I:⭐⭐⭐] verify 后不留持久层**：`destroy` 必执行
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 宿主机零挂载**：禁止 bind mount 宿主机目录

### 16.2 网络隔离

- **[P1] [C:⭐ I:⭐⭐⭐] 网络策略文档**：README 说明「无网络 = 无法 runtime pip」

### 16.3 资源隔离

- **[P1] [C:⭐ I:⭐⭐⭐] sandbox 健康探针** ✅：`sandbox_health.probe`（docker info + 镜像 + `network_mode=none` 冒烟）；`try_create_verifier` + `--health`
- **[P2] [C:⭐ I:⭐⭐⭐] 全局并发沙箱上限**：`FIXLOOP_MAX_SANDBOXES` 信号量
- **[P2] [C:⭐ I:⭐⭐⭐] pytest 超时兜底** ✅：`exit_code=-1` → 明确 `failure_logs`（pytest / pip install）；pip 超时跳过 pytest

### 16.4 权限降级

- **[P2] [C:⭐ I:⭐⭐⭐] 禁止特权与 Docker-in-Docker**：`Privileged=false`、不挂载 docker.sock

### 16.5 逃逸回归

- **[P2] [C:⭐ I:⭐⭐⭐] 逃逸回归 Case**：`case_adv_sandbox_*` 读 `/etc/passwd`、curl 外网、fork 爆炸

### 16.6 单 Turn 生命周期

- **[P1] [C:⭐⭐ I:⭐⭐⭐] cancel/timeout 统一 kill 路径**：`container.kill()` · 宿 workspace 回滚（[§12.7](#127-冲突--终止--恢复)）

### 16.7 工具执行沙箱分层

> 设计见 [DESIGN §16.7](bonus/DESIGN.md#167-工具执行沙箱分层)。

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] run_shell 宿主机 vs sandbox_verify 容器**：L1 Gate+quota · L2 Docker；文档 threat model
- **[P2] [C:⭐ I:⭐⭐⭐] 工具沙箱 trace 分层**：`execution_tier=host|container` 进 trace/report

---

## 17. Patch 与 Verify

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] AST 语义等价校验**：suspect 函数结构 diff，输出 `semantic_ok|drift`

---

## 18. 敏感信息处理

- **[P2] [C:⭐ I:⭐⭐⭐] prompt 注入对抗 eval**：`case_adv_injection_*`
- **[P2] [C:⭐ I:⭐⭐⭐] trace 保留策略**：默认 30 天 TTL；本地可清理 `.agent/runs/`
- **[P2] [C:⭐ I:⭐⭐⭐] 敏感产物加密**：patch/issue 落盘可选 AES（opt-in）

---

## 19. 链路可观测

> 设计见 [DESIGN §19](bonus/DESIGN.md#19-链路可观测)。Eval 聚合见 [§20.3](#203-agent-性能量化--judge--检索质量)。

### 19.1 Run · Trace · Report

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 统一 run_id（UUID）**：L1 + L2 共用
- **[P1] [C:⭐ I:⭐⭐⭐] 结构化 JSON 日志**：`FIXLOOP_LOG=json`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] trace.jsonl gzip**：超 1000 行归档

### 19.2 Repair · Agent 指标

- **[P1] [C:⭐ I:⭐⭐⭐⭐] node_timings 标准化 schema**：`localize|retrieve|patch|verify|repair_total` ms
- **[P2] [C:⭐ I:⭐⭐⭐] 分 Agent token / latency 表**：`by_agent` 进 `report.json`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Prometheus `/metrics`**
- **[P2] [C:⭐ I:⭐⭐] Grafana dashboard JSON**：node_timings + sandbox_ms 面板

### 19.3 工具 · Gateway 指标

- **[P2] [C:⭐ I:⭐⭐⭐] Gateway 拒绝计数**：`permission_denied_by_tool`（[§7.2](#72-调度--审计)）

### 19.4 核心运行时指标监控

> 设计见 [DESIGN §19.4](bonus/DESIGN.md#194-核心运行时指标监控)。

- **[P1] 🔶 [C:⭐⭐ I:⭐⭐⭐⭐] Context + cache 指标进 report/trace**：`context_sections`/`context_built` trace 已接线；`report.json` 聚合与 `cache_hit_rate` 待补（`3c67e15`）
- **[P1] ✅ [C:⭐⭐ I:⭐⭐⭐⭐] TTFT / 首字延迟**：streaming `ttft_ms`；非 streaming `time_to_first_byte`；trace `model_first_token`（`master` PR #93）
- **[P1] [C:⭐ I:⭐⭐⭐⭐] Retry 指标统一**：`parse_retry_count` · `attempts` vs `tool_steps` · L2 `retry_count`
- **[P1] [C:⭐ I:⭐⭐⭐⭐] Tool 步数 + 配额利用率**：`tool_steps` · `writes_used`/`shell_used` vs 硬顶（[§9](#9-硬上限与工具配额)）

---

## 20. 消融实验与评测

### 20.1 Case 库

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Case 011–020**：按错误类型矩阵扩展
- **[P1] [C:⭐ I:⭐⭐⭐⭐] 难度重标定**：依据 60 runs 上调或标 `requires_retriever`
- **[P2] [C:⭐ I:⭐⭐⭐] 负样本 Case**：ambiguous issue，期望 `exhausted`
- **[P3] [C:⭐⭐ I:⭐⭐] 多语言 Case**：Node + `language: java`

### 20.2 Runner 与指标

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] patch_precision 进 eval report 一等公民**：`min_lines.txt` / `actual_lines` → by_type 分桶
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] patch_equivalence_score**：actual vs expected → `full|partial|none`
- **[P1] [C:⭐ I:⭐⭐⭐⭐] `--resume` 断点续跑**：跳过已完成 `(variant, case, rep)`
- **[P2] [C:⭐ I:⭐⭐⭐] Pass@k**：同 Case 跑 k 次，报告 pass@1 / pass@3

### 20.3 Agent 性能量化 · Judge · 检索质量

> 设计见 [DESIGN §20.4](bonus/DESIGN.md#204-agent-性能量化--judge--检索质量)。运行时指标来自 [§19.4](#194-核心运行时指标监控)。

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] eval_report 性能矩阵**：fix_rate · patch_precision · avg_context_tokens · avg_cache_hit_rate · p50_ttft_ms · avg_tool_steps · avg_repair_retries（skill 见 [§13.5](#135-skill-召回率--版本)）

---

## 21. CLI · REPL（本地）

- **[P1] [C:⭐ I:⭐⭐⭐⭐] repair 退出码**：0 成功 / 1 失败 / 2 配置 / 3 超时
- **[P1] [C:⭐ I:⭐⭐⭐] 命令历史**：`readline` + Ctrl-R
- **[P2] [C:⭐⭐ I:⭐⭐⭐] /save /load /sessions /replay /prompt**：会话迁移、trace 回放、prompt 调试
- **[P2] [C:⭐ I:⭐⭐] 多行输入**：`\` 续行

---

## 22. 意图识别与路由

> 设计见 [DESIGN §23](bonus/DESIGN.md#23-意图识别与路由)。

### 22.1 L2 Issue 意图（Repair 入口）

- **[P1] [C:⭐ I:⭐⭐⭐⭐] `_parse_issue` 规则补全**：`test_failure` · `logic_error` 启发式 · 多文件 stack `suspect_files` + 行号
- **[P2] [C:⭐⭐ I:⭐⭐⭐⭐] 歧义 issue LLM 分类 fallback**：规则 → `unknown` 时 light_client JSON；失败保持 unknown + warn
- **[P2] [C:⭐ I:⭐⭐⭐] 语言检测** ✅：`language_detect`（shebang / 扩展名 / 关键字）；`RepairPlan.language` + `language_source`；Skill 按 language 过滤

### 22.3 意图 → 编排路由

> Agent 裁剪 · subtasks 见 [§12.1](#121-角色与机制) · [§12.8](#128-子问题拆分)；prompt 见 [§5.2](#52-l2-角色-prompt)。

- **[P1] [C:⭐ I:⭐⭐⭐⭐] issue_type → prompt 变体自动选择** ✅：`prompt_router` 集中路由；`patcher_suffix` + `localizer_hints`；trace `prompt_routing`

### 22.5 可观测与评测

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 意图识别进 trace/report**：`repair_plan` · `matched_skill` · `issue_type` · `intent_parser`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 意图对抗 eval Case**：`case_adv_ambiguous_*` · `case_adv_misleading_type_*`

### 22.2 Skill 策略匹配

> 主条目：[§13.2](#132-匹配算法)

- **[P2] [C:⭐ I:⭐⭐⭐] Skill 匹配置信度进 trace**：`matched_skill` · `trigger_pattern` · default 策略名

### 22.4 L1 会话意图（REPL / Memory）

- **[P2] [C:⭐ I:⭐⭐⭐] repair 启动写 task_summary**：`_parse_issue` 摘要进 working memory，供 [§4.4](#44-召回与-context-投影) 检索

---

## 23. 演示 · 文档 · 测试

- **[P1] [C:⭐ I:⭐⭐⭐] CLI 退出码单测**
- **[P2] [C:⭐ I:⭐⭐] Skill 匹配 / Skill 命中单测**

---

## 24. 输出质量 · 幻觉探针 · Judge Eval

> 设计见 [DESIGN §26](bonus/DESIGN.md#26-输出质量--幻觉探针--judge-eval)。

### 24.1 Verify 前 Sanity 闸口

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 改无关文件检测 + faithfulness Case**：patch ⊆ suspect_files ∪ related_tests；`case_adv_hallucination_*` · badcase tag（[§15.1](#151-bad-case-闭环)）

### 24.2 LLM-as-Judge（eval 变体）

- **[P2] [C:⭐⭐ I:⭐⭐⭐] eval 变体 `with_judge`**：optional judge_client · score+reason · 与 patch_precision 对照

---

*待办清单 · `✅`/`🔶` 标记完成情况（保留全部条目）· 设计见 [bonus/DESIGN.md](bonus/DESIGN.md)*
