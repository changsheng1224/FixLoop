# FixLoop Bonus 功能探索（合并版）

> 原 `bonus_layer1_plan.md` · `bonus_layer2_plan.md` · `bonus_m5-m6.md` · `bonus_m7-m8.md` · 项目级 `bonus.md` **已合并去重**。  
> 基线：`master` @ PR #84 · `agent_runtime/` + `src/` · **484 tests**。  
> 格式：**[P?] [C:复杂度 I:面试/展示价值] 标题**：简要方案。标注 **✅** 表示已有基础实现，条目为增强。

---

## 目录

| 部分 | 范围 |
|------|------|
| [I. Layer 1 运行时](#i-layer-1-运行时-agent_runtime) | `agent_runtime/` |
| [II. Layer 2 修复流水线](#ii-layer-2-修复流水线-src) | `src/` 多 Agent repair |
| [III. 评测与交付 M7–M8](#iii-评测与交付-m7m8) | `src/eval/` |
| [IV. 项目级工程](#iv-项目级工程) | 配置、多租户、压测、并发 |
| [V. 高价值扩展](#v-高价值扩展) | 跨层技术能力 + 演示/叙事 |

---

# I. Layer 1 运行时 (`agent_runtime/`)

## I.1 工具系统 — `tools.py` / `tool_executor.py` / `schema_utils.py`

- **[P1] [C:⭐ I:⭐⭐⭐] search 正则模式**：`regex=True` 用 `re.search` 替代子串匹配
- **[P1] [C:⭐ I:⭐⭐⭐] 审批时 diff 预览**：write_file / patch_file 审批时显示 patch 前后片段
- **[P1] [C:⭐ I:⭐⭐⭐] write_file 原子写**：先写 `.tmp` 再 `replace`
- **[P2] [C:⭐ I:⭐⭐⭐] search 结果上限**：`max_results` + 截断提示
- **[P2] [C:⭐ I:⭐⭐] list_files glob / depth**：`pattern="*.py"`、`depth=1` 限制递归
- **[P2] [C:⭐⭐ I:⭐⭐⭐] patch_file 统一 diff**：一次多文件多 hunk
- **[P2] [C:⭐⭐ I:⭐⭐] 文件编码检测**：`chardet` 非 UTF-8 转换
- **[P2] [C:⭐ I:⭐⭐⭐] permission_denied 写 trace**：`tool_policy` 拒绝时 `_emit` 审计事件
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 快照性能优化**：Gate 8 仅追踪 recent_files + 目标 path，非全树 SHA256

## I.2 安全 — `security.py` / `tool_context.py`

- **[P2] [C:⭐ I:⭐⭐⭐] 符号链接逃逸检测**：`ToolContext.resolve` 二次 `resolve()` 校验

## I.3 模型客户端 — `providers/clients.py` / `bootstrap.py`

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐] Ollama / OpenAI streaming**：SSE/chunk 增量解析，REPL 实时输出
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 多 Provider 独立熔断**：每 Client 独立 `CircuitBreaker`
- **[P2] [C:⭐ I:⭐⭐⭐] Retry-After + jitter**：429 退避加随机抖动
- **[P3] [C:⭐⭐ I:⭐⭐] HTTP keep-alive**：同 session 连接复用

## I.4 CLI / REPL — `cli.py`

- **[P1] [C:⭐ I:⭐⭐⭐] 命令历史**：`readline` + Ctrl-R
- **[P1] [C:⭐ I:⭐⭐⭐] `/memory` 真实输出**：渲染 working/episodic/durable（当前为占位）
- **[P2] [C:⭐ I:⭐⭐] 多行输入**：`\` 续行
- **[P2] [C:⭐⭐ I:⭐⭐⭐] /save /load /sessions /replay /prompt**：会话迁移、run 列表、trace 回放、prompt 调试
- **[P2] [C:⭐⭐ I:⭐⭐⭐⭐] REST API**：`--serve :8000`，POST `/ask` + GET `/session/{id}`
- **[P2] [C:⭐ I:⭐⭐⭐] ✅ `--health` / `--profile` 增强**：health 增 provider ping；profile 文档化 dev/prod/ci

## I.5 进度回调 — `callbacks.py`

- **[P1] [C:⭐ I:⭐⭐⭐⭐] 补全 AgentLoop 触发**：`on_step_start` / `on_final_answer` / native 路径统一 invoke

## I.6 熔断与回放 — `circuit_breaker.py` / `replay.py`

- **[P3] [C:⭐⭐ I:⭐⭐] 熔断事件 trace**：`circuit_opened` / `circuit_closed`

## I.7 持久化 — `task_state` / stores / `checkpoint.py`

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] checkpoint 每轮创建**：工具执行后 `trigger=step_end`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] trace.jsonl gzip**：超 1000 行归档
- **[P2] [C:⭐ I:⭐⭐⭐] run_id 改 UUID**：防并发碰撞
- **[P2] [C:⭐ I:⭐⭐] SessionStore 损坏恢复**：`.bak` 或跳过告警

## I.8 上下文工程 — `context_manager.py`

> **设计边界**  
> Context = **本轮 prompt 里能塞什么**；受 tiktoken 硬预算约束，不是无限窗口。  
> **裁剪优先级**（高→低）：`request`（永不裁）→ `prefix` → `memory` → `relevant` → `history`。  
> **与 Memory 分工**：Memory 是跨轮存储；Context 是 Memory + History + Workspace 的**当轮投影**，压缩会丢信息。  
> **与 L2 分工**：L1 `ContextManager` 服务 ReAct Agent；L2 Orchestrator 手工拼 prompt（见 II.4），应复用同一预算原则。  
> **现状 ✅**：5 section 组装 · `TokenBudget.fit` · LLM/规则双模 history 压缩 · `_summary_cache` 进程内缓存 · `TOOL_TRUNCATION` 按工具类型截断。

### I.8.1 Token 预算与 section 模型

- **[P1] [C:⭐ I:⭐⭐⭐] 多模型 tokenizer 切换**：未知模型 fallback cl100k_base + warn
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] memory section 优先级排序**：超总预算时按 task > modified > read > old summaries 取舍
- **[P2] [C:⭐ I:⭐⭐⭐] section 硬顶 + 总预算双限**：各 section 先 fit 到 `BUDGET_*`，再参与 `TOTAL_BUDGET=6000`，防 prefix 独占
- **[P2] [C:⭐ I:⭐⭐⭐] 摘要质量 metric + 轮数触发**：记录压缩比；history 轮数 >10 亦触发摘要（不只 token 阈值）

### I.8.2 History 压缩与摘要

- **[P1] [C:⭐ I:⭐⭐⭐⭐] ✅ LLM 摘要 + 规则降级**：超 2600 token 触发 `_maybe_summarize_history`；失败保留最近 8 条
- **[P1] [C:⭐ I:⭐⭐⭐] 摘要缓存持久化**：`_summary_cache` 落盘 `.agent/summary_cache/`，key=history 片段 hash
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 增量压缩**：在已有 `[Earlier summary]` 上追加新条目，避免每轮全量重摘要
- **[P2] [C:⭐ I:⭐⭐⭐] Error/Traceback/FAILED 强制保留**：压缩时不丢弃含错误信号的 tool/user 行
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 重复 read_file 合并**：同 path 多次 read 合并为「最后读取 + 行号范围」
- **[P2] [C:⭐ I:⭐⭐] KEEP_RECENT_HISTORY 可配置**：默认 6 条完整保留，eval 长会话可调大

### I.8.3 工具结果进入 context

- **[P1] [C:⭐ I:⭐⭐⭐] ✅ 按工具类型截断**：`TOOL_TRUNCATION`（search 800 / read_file 2000 等）
- **[P2] [C:⭐ I:⭐⭐⭐] 重要行优先**：Error、路径、行号行排在截断结果前部

### I.8.4 Relevant 检索（与 Memory 衔接）

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 混合检索重排**：episodic 关键词 + semantic cosine 加权 merge，top-k 带 score
- **[P2] [C:⭐ I:⭐⭐⭐] relevant 无 query 降级**：用 `task_summary` 作检索 key
- **[P2] [C:⭐ I:⭐⭐] 检索失败静默降级**：Semantic 模型不可用时不阻塞 `build()`，relevant 留空

### I.8.5 Prefix、Workspace 与 Prompt Cache

- **[P1] [C:⭐ I:⭐⭐⭐⭐] ✅ prompt_cache_key = prefix hash**：metadata 透传 Client
- **[P2] [C:⭐ I:⭐⭐⭐] workspace 文档按需注入**：`workspace.text()` 超 N token 只保留 AGENTS.md + README 头；其余给路径

### I.8.6 可观测与调试

- **[P2] [C:⭐ I:⭐⭐⭐] build() metadata 进 trace/report**：sections token 数、`cuts[]`、total/budget

## I.9 Prompt — `prompt_prefix.py`

- **[P1] [C:⭐ I:⭐⭐⭐⭐] Cache 命中率**：`/session` 显示 cache_hits/misses
- **[P2] [C:⭐⭐ I:⭐⭐⭐] few-shot / rules 外置**：`.agent/examples.md`、`.agent/rules.md`

## I.10 解析与控制循环 — `runtime.py` / `agent_loop.py`

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 批量工具调用**：多 `<invoke>` / JSON array 一次执行
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 单步工具超时**：`concurrent.futures` 防 hang
- **[P1] [C:⭐⭐⭐⭐ I:⭐⭐⭐⭐] 流式文本解析**：逐 chunk 识别 `<tool>` / `<final>`
- **[P1] [C:⭐⭐ I:⭐⭐⭐] ✅ retry 指数退避**：可配置 `--retry-max-delay`
- **[P2] [C:⭐⭐⭐⭐⭐ I:⭐⭐⭐] 并行工具执行**：无冲突 tool 同轮 parallel
- **[P2] [C:⭐ I:⭐⭐⭐] stop_reason 枚举**：`final|step_limit|circuit_breaker|parse_fail`
- **[P2] [C:⭐ I:⭐⭐] 解析错误精确定位 / XML 属性格式补全**
- **[P3] [C:⭐⭐ I:⭐⭐⭐] CoT 提取**：thinking 块剥离后再进 history

## I.11 配置 — `config.py`

- **[P2] [C:⭐ I:⭐⭐⭐] AgentConfig 扩展**：`total_token_budget`、`tool_timeout`、`semantic_memory_enabled`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] REPL 热重载**：`/config max_steps=10`

## I.12 记忆系统 — `features/memory/`

> **设计边界**  
> 四层模型：**Working**（会话内任务/最近文件）· **Episodic**（工具笔记，带上限）· **Durable**（跨会话 Markdown topic）· **Semantic**（embedding 检索）。  
> **不是源码真相**：记忆可 stale；读文件 / AST / pytest 才是 ground truth。  
> **有上限**：`MAX_RECENT_FILES=8`、`MAX_EPISODIC_NOTES=12` 等，超出裁剪或归档。  
> **不防幻觉**：检索到的「相似修复」可能误导 Patcher，需 confidence + eval 约束。  
> **与 L2 分工**：L1 memory 服务 ReAct；L2 用 `RepairState` / Blackboard / `RetrievedContext`（II.4），修复成功可 **promote 回** durable。  
> **现状 ✅**：`update_memory_after_tool` · `promote_durable_memory` · `DurableMemoryStore` · Semantic 懒加载 + HF 离线策略。

### I.12.1 Working Memory — `working.py`

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] ✅ task_summary**：`_gen_task_summary` 用 light_client；可增失败率 metric
- **[P2] [C:⭐ I:⭐⭐⭐] recent_files 时间戳 + LRU**：`last_access` 排序；超 `MAX_RECENT_FILES` 淘汰最旧
- **[P2] [C:⭐ I:⭐⭐⭐] file_summaries TTL**：30min 未引用清理；write/patch 后 `invalidate_file_summary`

### I.12.2 Episodic Memory — `episodic.py`

- **[P2] [C:⭐ I:⭐⭐⭐] 去重增强**：同 text 相邻去重（已有）；扩展为同 `(tool, path)` 合并
- **[P2] [C:⭐⭐ I:⭐⭐⭐] episodic → durable 晋升**：kind=decision 且多次被检索 → 自动 promote

### I.12.3 Durable Memory — `durable.py`

- **[P2] [C:⭐ I:⭐⭐⭐] 全文索引**：`.agent/memory/index.json` 加速 `retrieval(query)`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 冲突检测与合并**：同 topic 相似条目提示合并或版本链

### I.12.4 Semantic Memory — `semantic.py`

- **[P2] [C:⭐ I:⭐⭐⭐] embedding 缓存**：同文本 content_hash 不重复 encode；落盘 `.agent/embed_cache/`
- **[P2] [C:⭐ I:⭐⭐⭐] 相似度阈值可配**：低于 threshold 不注入 relevant，防噪声

### I.12.5 跨层边界与生命周期

- **[P2] [C:⭐⭐ I:⭐⭐⭐] L2 repair 写回 durable**：`status=fixed` → issue 摘要 + patch 路径（II.3 / II.4 衔接）
- **[P2] [C:⭐⭐ I:⭐⭐⭐] L1 durable 注入 L2**：repair 启动读 topics/ → `similar_fixes`
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐] 记忆优先级仲裁**：超 800 token 投影时 task > files > summaries > episodic

### I.12.6 运维与 REPL

- **[P1] [C:⭐ I:⭐⭐⭐] `/memory` 真实输出**：working / episodic / durable 分块展示（当前占位）
- **[P2] [C:⭐ I:⭐⭐⭐] `/memory forget`**：清 episodic 或按 topic 删 durable

## I.14 L1 整体增强

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 统一 logger + `--log-level`**
- **[P2] [C:⭐ I:⭐⭐⭐] `agent.register_tool` 动态扩展**
- **[P3] [C:⭐⭐⭐⭐ I:⭐⭐⭐] 同进程多 Agent 隔离 session/quota**

---

# II. Layer 2 修复流水线 (`src/`)

> **已在 PR #83**：`tool_policy`、`VerifyStrategy`、`RepoPatchApplier`、`RepairPipelineMixin`、`output_parsers`、`repo_snapshot`、baseline factory。

## II.1 Orchestrator — `orchestrator.py` / `repair/pipeline.py`

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Prompt / Issue 解析外置**：`repair/prompts.py`、`repair/issue_parser.py`，Orchestrator 只编排
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 分阶段超时**：localize / patcher / verify 独立 timeout
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 反馈环增强**：失败测试 + 上轮改动 + 回滚提示 + build_log → `state.feedback`
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] L2 阶段 checkpoint**：每阶段写 `repair_checkpoint.json`，`--resume-repair <run_id>`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] JSON 失败自动重试**：空 suspects/patches 时再 ask 并附格式约束
- **[P2] [C:⭐ I:⭐⭐⭐] Retriever 失败降级**：`search`/`rg` 按堆栈文件名补上下文
- **[P2] [C:⭐ I:⭐⭐⭐] CLI `--max-retries` / 全流程 `--timeout`**
- **[P2] [C:⭐ I:⭐⭐⭐] 超时预算树**：全局 deadline 按阶段分配剩余时间
- **[P2] [C:⭐⭐ I:⭐⭐] asyncio 流水线（可选）**：当前 ThreadPool 已满足 M6
- **[P2] [C:⭐ I:⭐⭐] 删解析薄包装**：pipeline 直调 `output_parsers`

## II.2 状态与 Blackboard — `state.py`

- **[P1] [C:⭐ I:⭐⭐⭐⭐] schema 版本迁移**：`from_dict` 按版本升级
- **[P2] [C:⭐ I:⭐⭐⭐] repair 落盘**：`.agent/repairs/{id}/repair_state.json` + timings

## II.3 Skill — `src/skills/*.yaml`

- **[P1] [C:⭐ I:⭐⭐⭐] Skill 注入 Prompt**：`example_patch` / `suggested_tools` 写入 `[Skill 提示]`
- **[P2] [C:⭐ I:⭐⭐] 匹配 priority / 最长 pattern 优先**
- **[P2] [C:⭐ I:⭐⭐⭐] 命中率统计**：`matched_skill` 进 `node_timings`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 修复写回 durable**：`status=fixed` 追加 issue + patch 笔记
- **[P3] [C:⭐⭐ I:⭐⭐] YAML 热加载**

## II.4 记忆 × 上下文 — L2 Repair Context

> **设计边界**  
> L2 **不用** L1 `ContextManager.build()`，而由 Orchestrator 手工拼 Localizer/Retriever/Patcher/Verifier prompt。  
> 结构化状态在 **`RepairState` / Blackboard`**；自然语言仅在 prompt 边界内。  
> **钉扎区**：issue + stack 永不截断； suspects / tests / feedback 可裁剪。  
> **不重复读**：Localizer∥Retriever 结果进 Blackboard 去重；同一 file 只读一次进 Patcher。  
> **记忆来源**：当前 repo 内容 > L1 durable 相似修复 > Skill 示例 > 模型先验。

### II.4.1 ContextPack 与 token 预算

- **[P1] [C:⭐ I:⭐⭐⭐⭐] issue/stack 钉扎区**：Patcher/Localizer prompt 中全文保留，其余块 `fit(token_limit)`
- **[P2] [C:⭐ I:⭐⭐⭐] 分 Agent 预算表**：Localizer 2k / Retriever 3k / Patcher 4k / Verifier 1k（可 yaml 配置）
- **[P2] [C:⭐ I:⭐⭐⭐] 上下文快照落盘**：`.agent/repairs/{id}/context_pack.json` 每轮或每阶段
- **[P2] [C:⭐ I:⭐⭐⭐] tiktoken 复用 L1**：Orchestrator 调 `TokenBudget.fit` 统一裁剪逻辑

### II.4.2 Blackboard 与去重

- **[P2] [C:⭐ I:⭐⭐⭐] Localizer∥Retriever 去重**：相同 `file_path` + 行号合并；Blackboard 唯一 suspect 列表
- **[P2] [C:⭐ I:⭐⭐] Blackboard 冲突检测增强**：互斥 suspect 置信度比较 + trace 记录

### II.4.3 检索上下文 — Retriever / Memory

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 历史修复检索**：SemanticMemory / episodic → `RetrievedContext.similar_fixes`
- **[P1] [C:⭐⭐ I:⭐⭐⭐] Retriever embedding 缓存**：`.agent/embed_cache/` 按 `(repo_hash, file, content_hash)`
- **[P2] [C:⭐ I:⭐⭐⭐] `_read_test_context` 上限**：每测试文件 `max_lines` / max_chars，防灌爆 Patcher
- **[P2] [C:⭐ I:⭐⭐⭐] Retriever 失败降级**：`search`/`rg` 按堆栈文件名补 `related_tests`
- **[P3] [C:⭐⭐⭐ I:⭐⭐] 堆栈语义定位**：无 git 时 semantic 搜相似堆栈

### II.4.4 多轮 feedback 与 Skill

- **[P1] [C:⭐ I:⭐⭐⭐] feedback 滑动窗口**：最近 K 轮 verify 失败摘要 + 失败测试名集合

### II.4.5 L1 Memory 桥接

- **[P2] [C:⭐⭐ I:⭐⭐⭐] repair 启动读 durable topics**：预填 `similar_fixes`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] repair 成功写回 durable**：与 I.12.5 / II.3 同一 promote 管道
- **[P2] [C:⭐ I:⭐⭐] 不信任记忆覆盖 suspect**：similar_fixes 仅作 hint，Localizer 仍需 stack/AST 证据

## II.5 工厂与 Prompt — `repair_factory.py` / `prompts/`

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 角色级模型路由**：Localizer 小模型、Patcher 强模型；report 分角色 token/latency
- **[P2] [C:⭐ I:⭐⭐⭐] Prompt 版本追踪**：`# version: N` 写入 trace
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Multi-Agent 降级 Single-Agent**：verify 连续失败后 `degraded_mode` + baseline
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 动态 max_steps**：按 issue 复杂度调整

## II.6 Patch 与 Verify — `repair/patch_applier.py` / `repair/verify.py`

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] AST 语义等价校验**：suspect 函数结构 diff，输出 `semantic_ok|drift`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] patch 干跑**：内存 apply + AST 校验 + diff 预览后审批落盘
- **[P2] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 容器内打补丁**：`--patch-target container`，verify 后 `docker cp` 导出

## II.7 Docker 沙箱 — `harness/` / `sandbox_tools.py`

> **设计边界（面试口径）**  
> 沙箱目标：**文件系统隔离 · 网络隔离 · 资源隔离 · 权限降级**——限制 Agent 生成代码/测试对**宿主机与外部**的危害。  
> **不防逻辑错误**：错误 patch 若仍通过 pytest，沙箱无法判定业务语义；需 II.6 AST 等价、人工 review、eval Case 约束。  
> **有代价**：每 Turn 的 create / tar / exec 延迟；隔离越严，合法构建（如 pip）越难，需镜像预装或离线 wheel。  
> **有逃逸风险**：内核漏洞、Docker 配置失误、挂载/socket 泄露、特权镜像——应文档化 threat model，**不声称「绝对安全」**。  
> **现状 ✅**：`network_mode=none` · `mem_limit`/`cpu_quota` · tar 传 `/code`（无 bind mount）· 单 Turn 结束 `destroy`。

### II.7.1 文件系统隔离

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 仅暴露 `/code` + `/tmp` 可写**：只读 rootfs（`read_only=True` + tmpfs `/tmp`），禁止写 `/etc`、`/root`；tar 只解压到 `/code`
- **[P1] [C:⭐ I:⭐⭐⭐] tar 排除与大小上限**：打包前排除 `.git`、`.venv`、`node_modules`；超 N MB 拒绝或白名单路径，防 tar 炸弹拖垮宿主机
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 容器内路径锚定**：`entrypoint.sh` / PatchApplier 仅接受 `/code/...` 相对路径，拒绝 `..` 与绝对路径越界
- **[P2] [C:⭐ I:⭐⭐⭐] verify 后不留持久层**：`destroy` 必执行；温池 borrow 结束仍 reset 文件系统或换容器，防跨 Turn 状态泄露
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 宿主机零挂载**：禁止 bind mount 宿主机目录进容器（当前 tar 方案保持）；文档明确 Windows 亦同

### II.7.2 网络隔离

- **[P1] [C:⭐ I:⭐⭐⭐⭐] ✅ 默认 `network_mode=none`**：pytest/build 无外网；依赖必须在镜像内预装或离线 wheel 打入 tar
- **[P1] [C:⭐ I:⭐⭐⭐] 网络策略文档**：README 说明「无网络 = 无法 runtime pip」；提供 `docker build` 预装 deps 的标准 Dockerfile 流程
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 可选 profile 显式开网**：Node/npm 等必须联网时单独 profile + opt-in env，默认仍 none

### II.7.3 资源隔离

- **[P1] [C:⭐ I:⭐⭐⭐] ✅ mem_limit / cpu_quota**：已有 4g / 200000；写入 `fixloop.yaml` 可配，防单 Case 吃光宿主机
- **[P1] [C:⭐ I:⭐⭐⭐] sandbox_timings 进 report**：`container_create_ms` / `tar_copy_ms` / `pytest_ms` 量化隔离开销，支撑容量规划（IV.3 压测）
- **[P2] [C:⭐ I:⭐⭐⭐] 全局并发沙箱上限**：`FIXLOOP_MAX_SANDBOXES` 信号量，防 Docker daemon 被并发 verify 打满
- **[P2] [C:⭐ I:⭐⭐⭐] pytest 超时兜底**：`exit_code=-1` 仍生成明确 `failure_logs`（超时属资源隔离，非逻辑正确性）

### II.7.4 权限降级

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 非 root 运行**：镜像 `USER repair`（uid≠0），`entrypoint.sh` 与 pytest 均以低权用户执行
- **[P2] [C:⭐ I:⭐⭐⭐] 禁止特权与 Docker-in-Docker**：创建容器时断言 `Privileged=false`、不挂载 `/var/run/docker.sock`
- **[P2] [C:⭐ I:⭐⭐⭐] 最小镜像 attack surface**：slim 基础镜像、固定 digest pin，CI 扫描已知 CVE（文档 + 可选 trivy job）

### II.7.5 开销、逃逸与「不防逻辑错误」

- **[P2] [C:⭐⭐ I:⭐⭐⭐] 温容器池（权衡项）**：复用降延迟但增加跨 Turn 文件残留风险；borrow 必须 reset 或文件系统隔离验收测试
- **[P2] [C:⭐ I:⭐⭐⭐] 逃逸回归 Case**：`case_adv_sandbox_*` 尝试读 `/etc/passwd`、curl 外网、fork 爆炸；期望被隔离层拦截或超时
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 镜像预热检查**：缺失则打印 build 指引，避免半残镜像导致「假通过/假失败」

### II.7.6 构建 / 验证 / 补丁流水线 — `python_runner.py` / `patch_applier.py` / `sandbox_tools.py`

> **分工**：Harness 负责「在隔离环境里跑 build+test+patch」；**不**判断 patch 语义是否正确（见 II.6）。

- **[P1] [C:⭐ I:⭐⭐⭐] ✅ 单 Turn 生命周期**：`create` → 可选 `pip install` → `pytest --json-report` → `destroy`；`_sandbox_id` 同 Turn 内复用 build

## II.8 工具与权限 — `tools/` / `middleware.py`

- **[P1] [C:⭐ I:⭐⭐⭐] Retriever 规则快路径**：`--fast-retrieve` 跳过 LLM
- **[P2] [C:⭐ I:⭐⭐⭐] Localizer 工具顺序**：stack_parse → ast_parse；违规 warn
- **[P2] [C:⭐ I:⭐⭐⭐] ast_parse 局部解析**：仅 suspect 行附近 AST
- **[P2] [C:⭐ I:⭐⭐⭐] ToolGateway 越权审计**：`permission_denied` → trace / agent_errors

## II.9 安全与结构化输出

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] eval 对抗 Case 套件**：`case_adv_*` prompt injection / 路径遍历，CI 必跑

## II.10 CLI — `src/cli.py`

- **[P1] [C:⭐ I:⭐⭐⭐⭐] repair 退出码**：0 成功 / 1 失败 / 2 配置 / 3 超时
- **[P2] [C:⭐ I:⭐⭐⭐] Verifier fallback 可观测**：verbose 说明 Docker 不可用


## II.12 测试与文档

- **[P1] [C:⭐ I:⭐⭐⭐] CLI 退出码单测**
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] `@pytest.mark.docker` 集成测**
- **[P2] [C:⭐ I:⭐⭐] Skill 匹配 / Skill 命中单测**

---

# III. 评测与交付 M7–M8

## III.1 Case 库 — `src/eval/cases/`

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] Case 011–020**：按错误类型矩阵扩展
- **[P1] [C:⭐ I:⭐⭐⭐⭐] 难度重标定**：依据 60 runs 上调或标 `requires_retriever`
- **[P2] [C:⭐ I:⭐⭐⭐] 负样本 Case**：ambiguous issue，期望 `exhausted`
- **[P2] [C:⭐ I:⭐⭐⭐] metadata 扩展**：`requires_retriever`、`flaky`、`tags`
- **[P3] [C:⭐⭐ I:⭐⭐] 多语言 Case 种子**：Node + `language: javascript`

## III.2 评分与 Runner

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] patch_equivalence_score**：actual vs expected → `full|partial|none`
- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 并行跑 Case**：`run_all(workers=N)` + temp 隔离 + API 限流
- **[P1] [C:⭐ I:⭐⭐⭐⭐] `--resume` 断点续跑**：跳过已完成 `(variant, case, rep)`
- **[P2] [C:⭐ I:⭐⭐⭐] Pass@k**：同 Case 跑 k 次，报告 pass@1 / pass@3

## III.3 指标、Baseline 与回归

- **[P2] [C:⭐ I:⭐⭐⭐] 分 Agent token 表**：ablation summary `by_agent`

---

# IV. 项目级工程

## IV.1 配置与插件

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 全局 `fixloop.yaml`**：env > 项目 > 用户 > 默认；pydantic 校验
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Skill 包目录**：`~/.fixloop/skills/` 覆盖
- **[P2] [C:⭐⭐ I:⭐⭐⭐] entry_points 插件**：`fixloop.tools` / verify / orchestrator variants

## IV.2 可观测性

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 统一 run_id**：L1 + L2 共用 UUID
- **[P1] [C:⭐⭐ I:⭐⭐⭐] 结构化 JSON 日志**：`FIXLOOP_LOG=json`
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Prometheus `/metrics`**

## IV.3 压测与容量

- **[P1] [C:⭐⭐ I:⭐⭐⭐⭐] 压测场景库 + Locust/k6 驱动**
- **[P1] [C:⭐⭐ I:⭐⭐⭐] 沙箱池饱和压测 + 规格表**
- **[P2] [C:⭐⭐ I:⭐⭐⭐] Provider 429 模拟 / 磁盘压测 / 夜间 perf CI**

## IV.4 多用户隔离

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 租户命名空间**：`.agent/tenants/<id>/`
- **[P2] [C:⭐ I:⭐⭐⭐] 公平调度 weighted queue**

## IV.5 并发与伸缩

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] 无状态 worker + Redis 状态**
- **[P1] [C:⭐⭐ I:⭐⭐⭐] `.agent` 写锁 / 全局 inflight 上限**
- **[P2] [C:⭐⭐ I:⭐⭐⭐] 任务队列 + `/runs/{id}` 轮询**
- **[P2] [C:⭐ I:⭐⭐⭐] 取消与超时级联 cancel**
- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] SSE 阶段事件流**：`GET /repair/{id}/events`

## IV.6 可靠性、缓存与契约

- **[P1] [C:⭐⭐⭐ I:⭐⭐⭐⭐] repair 幂等键 + LRU 结果缓存**
- **[P2] [C:⭐ I:⭐⭐⭐] 增量 repo snapshot**：仅 hash 变更文件
- **[P2] [C:⭐ I:⭐⭐⭐] OpenAPI 化 serve 接口**

## IV.7 合规、脚手架与产物

- **[P2] [C:⭐ I:⭐⭐⭐] redact 策略表 / 离线模式 `FIXLOOP_OFFLINE=1`**

---

# V. 高价值扩展

> 面试向：**§V.1 可演示叙事** · **§V.2 跨层技术（与上文不重复的实现面）**

## V.1 演示与叙事（文档 / Demo / 指标口径）

- **[P1] [C:⭐ I:⭐⭐⭐⭐] `docs/EVAL_SUMMARY.md`**：60 runs、pass@1、1.22 vs 0.94、Case breakdown
- **[P1] [C:⭐ I:⭐⭐⭐⭐] 失败 taxonomy 自动统计**：parse / patch / verify / api / timeout 占比
- **[P1] [C:⭐ I:⭐⭐⭐⭐] Live Trace + 2 分钟 Demo**：`demo_trace_walkthrough.sh`、`make demo-interview`
- **[P1] [C:⭐ I:⭐⭐⭐] SLO / Error Budget 一页**
- **[P2] [C:⭐ I:⭐⭐⭐] 上下文预算可视化 / Prompt A/B**
- **[P2] [C:⭐ I:⭐⭐⭐] 交互式架构页 / Case 解剖卡片**

## V.2 跨层技术补充

- **[P2] [C:⭐ I:⭐⭐] patch 流式预览**（streaming parser 增量 diff hunks）
- **[P2] [C:⭐ I:⭐⭐] 多副本 trace 存储一致性**（UUID + 租户前缀）

---

*合并版 · base `master` @ PR #84 · 484 tests*
