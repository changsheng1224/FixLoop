# FixLoop Bonus 面试加分与补债条目（bonus4）

> **仅 backlog（面试选型 + 补债）**；设计见 [docs/bonus/DESIGN.md](bonus/DESIGN.md)。  
> **产品边界**：本地 CLI / REPL + `src.cli repair`；不实现 Web / HTTP / 多租户。  
> 基线：`master` @ V1.4-Bonus15 · **1581 tests**。  
> **条目格式**（Superpowers 选型后）：  
> `**[P?] [状态] [C:… I:…] 标题**`  
> `推荐实现：…`（含模块、步骤、字段、验收要点；**不含弃选方案**）  
> **完成标记**：`✅` · `🔶` · `❌`。同主题内 **P1 → P2 → P3**。  
> 开发：`brainstorming` → `writing-plans` → TDD → PR（动手前说明并获确认）。

---

## 目录

| 主题 | 说明 |
|------|------|
| [1. Agent Loop / ReAct](#1-agent-loop--react) | 取消、Plan、死循环、解析重试、Middleware |
| [2. Context 工程](#2-context-工程) | Section 预算、压缩、钉扎、diff-only |
| [3. 分层记忆](#3-分层记忆) | 写入/召回/Dream/路由表/检索指标 |
| [4. RAG / 检索](#4-rag--检索) | 三层 RAG、Agentic Retriever、Skill 向量 |
| [5. Multi-Agent 编排](#5-multi-agent-编排) | 动态裁剪、subtasks、Planner、去重、配额 |
| [6. Eval / 消融 / 可观测](#6-eval--消融--可观测) | Case、equivalence、naive 基线、trace/report、成本 |
| [7. CLI · 演示 · 文档](#7-cli--演示--文档) | streaming REPL、workspace、意图 fallback |
| [8. 安全 · 沙箱 · 工具](#8-安全--沙箱--工具) | 逃逸、shell、加密、schema CI |
| [9. Checkpoint · 续跑 · Session](#9-checkpoint--续跑--session) | resume、触发点、bak、/save |
| [10. Patch · JSON · 输出](#10-patch--json--输出) | 语义等价、json5、JSON mode、fuzz |

---

## 1. Agent Loop / ReAct

> 设计见 [DESIGN §2](bonus/DESIGN.md#2-agent-loop--react)。

- **[P1] 🔶 [C:⭐⭐ I:⭐⭐⭐⭐] 死循环检测 K 次 + `loop_detected`**
  - 推荐实现：在 `agent_runtime/config.py` 增加 `loop_detect_threshold: int = 3`。`ToolExecutor` 维护滑动窗口，对每次成功调度前的调用记 `(tool_name, canonical_args_hash)`；窗口内相同键次数 ≥ K 时返回专用 rejection 码。`agent_loop` 捕获后调用 `ts.stop_with_reason(StopReason.CIRCUIT_BREAKER)`，并 `_emit("loop_detected", {tool, count, args_hash})`。与 Gate5「连续 duplicate 拒再调」并存：Gate 挡单步，本项升 stop。单测：`tests/test_tool_executor.py` / `test_agent_loop.py` 连续 3 次同 `read_file` → stop + trace 含事件。
- **[P1] 🔶 [C:⭐⭐ I:⭐⭐⭐⭐] Plan/TodoList 强化**
  - 推荐实现：从 `AgentLoop.run()` 抽出 `_plan_phase(user_message)`：优先 `light_client` 产出 `[{id, content, status}]`，失败则规则拆句；写入 `session["plan_todos"]`；`_emit("plan_created")` 与 `plan_phase`。L2 `repair` 入口传 `skip_plan=True`。后续 step 经 `_get_state`/todo 投影。单测：普通 ask 有 `plan_created`；repair 路径无 plan LLM 调用。
- **[P1] ❌ [C:⭐⭐ I:⭐⭐⭐⭐] 空模型响应 → 重试 → `api_error`**
  - 推荐实现：`agent_runtime/errors.py` 定义 `EmptyModelResponse`；各 `ModelClient.complete*` 在 `not (raw or "").strip()` 时抛出。`agent_loop` XML/Native 调用处捕获：递增 `_empty_retries`，短 backoff 后**同 step 重调**（不推进 tool step）；达 `MAX_EMPTY_RETRIES`（建议 2–3）则 `stop_with_reason(API_ERROR, detail="empty_model_response_exhausted")`。与 parse `kind=empty`（有 HTTP body 无合法 tag）分轨。单测：连续 `""` → `api_error`；空一次后正常 → 成功。
- **[P1] 🔶 [C:⭐⭐ I:⭐⭐⭐⭐] Retry prompt 四段式**
  - 推荐实现：扩展 `parse_recovery.build_recovery_prompt`，输出固定 Markdown 四节：①上一次**成功** tool（`name`+args 摘要，来自 `ts.last_tool` 或 history 中 `tool_status=success`）②刚才输出（截断 raw）③错误位置（snippet + caret + `error_offset`）④正确格式（schema 样例）。`_handle_parse_retry` 传入 `last_tool_call`；`parse_retry` payload 增 `has_last_tool_anchor: bool`。单测：`test_parse_recovery` 断言四节均出现且含上次 tool 名。
- **[P2] 🔶 [C:⭐⭐ I:⭐⭐⭐] 流式模型 cancel**
  - 推荐实现：默认 `ask()` 走 `complete_stream`（`clients.py`）；chunk 循环每批检查 `cancel_token.is_cancelled`，立即 abort、关闭连接，再走既有 `_finish_user_cancel`（`stop_reason=user_cancel`，checkpoint `user_cancel`）。与 Ollama 流式路径行为对齐。单测：mock stream 中途 cancel → 不再读后续 chunk。
- **[P2] ❌ [C:⭐⭐ I:⭐⭐⭐] 空转 replan · todo blocked**
  - 推荐实现：`StepGuard` 判定 stall（连续 N 步无 `affected_paths` / 无进展）时，将当前 `in_progress` todo 标为 `blocked`，`_emit("todo_updated", {id, status})`，并在下一轮 observation/system 提示中注入「读 todo、考虑 replan」。不自动全量重跑 `_plan_phase`。单测：注入 stall → todo 状态变为 blocked。
- **[P2] 🔶 [C:⭐⭐ I:⭐⭐⭐] Middleware / Callback 链**
  - 推荐实现：`callbacks.py` 增加 `CallbackChain(callbacks: list[AgentCallback])`，`_notify(method, **kw)` 按序调用；任一环异常 log + 继续（可用 flag 改 fail-fast）。`AgentLoop` 只持有 Chain；`CLIProgressCallback` 固定为链末。覆盖 `pre_model`/`post_model`/`pre_tool`/`post_tool`/`on_step_start`/`on_final_answer`。单测：三 callback 调用顺序与短线跳过。
- **[P2] 🔶 [C:⭐ I:⭐⭐⭐] Native `context_built` 对齐**
  - 推荐实现：Native 路径 `ContextManager.build()` 成功后补 `_emit("context_built", {sections, token_counts, prefix_hashes, ...})`，字段与 XML 路径同一 schema，供 replay/--show-prompt。落点：`agent_loop` native 分支 `build` 之后、model 调用之前。单测：native fixture 断言事件存在且 sections 键齐全。
- **[P2] 🔶 [C:⭐ I:⭐⭐⭐] `final_answer` 失败回 Acting**
  - 推荐实现：Native/XML 校验 final 标签或可选 JSON schema 失败时，构造 `ParseRetry(build_recovery_prompt(...))`，走同源 `_handle_parse_retry`，回到 Acting，不直接 `_complete_run`。可选：L2 子任务启用 JSON mode final。单测：畸形 final → 再次 model 调用而非立刻结束。

---

## 2. Context 工程

> 设计见 [DESIGN §3](bonus/DESIGN.md#3-context-工程)。

- **[P1] ❌ [C:⭐ I:⭐⭐⭐⭐] context HARD_CAP 拒绝 ask**
  - 推荐实现：`ContextManager.build()` 合计超配置 `HARD_CAP`（默认 8000）抛 `ContextTooLargeError`（含 actual/limit）；`agent_loop` 捕获 → `stop_reason=context_overflow` + 用户可读错误。**拒绝 ask**，不做 silent truncate。单测：`test_context_manager` 超限抛错且 loop stop 原因正确。
- **[P1] 🔶 [C:⭐⭐ I:⭐⭐⭐⭐] state section 注入 plan/repair 摘要**
  - 推荐实现：`_get_state()` 序列化 `plan_todos` 前 3 条 `content`（含 status）+ 一行 `task_summary` / repair `phase`；经 `section_filler` 遵守 `BUDGET_STATE`。现状「仅计数」改为可读文本。单测：state 段含 todo 字串，超长时仍以预算截断后段。
- **[P1] 🔶 [C:⭐ I:⭐⭐⭐⭐] 钉扎区 enforce 单测**
  - 推荐实现：新建 `tests/test_tier_pins_enforce.py`：构造超长 history + 长 issue/suspect；调用 `fit_repair_user_prompt` / L0 裁剪后断言 issue 文本与 `suspect.file_path` 仍在 user prompt。配置源：`tier_pins.yaml` 的 pin 字段列表。
- **[P1] 🔶 [C:⭐⭐ I:⭐⭐⭐⭐] history 只读 JSONL**
  - 推荐实现：`ContextManager.build` 优先 `runtime.read_history_jsonl()`（`.agent/history.jsonl` append-only）；仅当文件缺失时 fallback 内存 session；**禁止** build 写回 JSONL。写路径仍由 loop/runtime 追加。单测：篡改 session 内存不影响 JSONL 投影结果。
- **[P1] 🔶 [C:⭐ I:⭐⭐⭐⭐] Section 硬顶 enforce**
  - 推荐实现：`section_filler.add_section` 任一超 `BUDGET_*` 立即 `_fit_section`，不只依赖 TOTAL；补优先级矩阵单测（request > prefix > memory > relevant > history）。模块：`section_filler.py`。
- **[P1] 🔶 [C:⭐⭐ I:⭐⭐⭐⭐] tier_pins L2**
  - 推荐实现：`fit_repair_user_prompt`（`repair_context_blocks.py`）与 L0 compression **共读**同一 `tier_pins.yaml`（如 `orchestrator_pin_fields`）；裁剪时跳过钉扎键。单测：pin 字段在超预算时不被删。
- **[P1] 🔶 [C:⭐ I:⭐⭐⭐] native 全 L0–L5**
  - 推荐实现：审计 `chat_with_native_tools` 调用链；history 缺压缩则接入既有 `compression_pipeline`（L0→…→L5），与 XML 共用入口函数。单测：native 长 history 触发压缩 metadata / `compression_triggered`。
- **[P1] ❌ [C:⭐ I:⭐⭐⭐] 摘要缓存持久化**
  - 推荐实现：`compression_pipeline` 将 `_summary_cache` 落盘 `.agent/summary_cache/{content_hash}.txt`；启动扫描加载；写失败静默降级内存 dict。单测：二次 build 同 hash 不调摘要 LLM（mock）。
- **[P2] ❌ [C:⭐⭐ I:⭐⭐⭐] 增量摘要**
  - 推荐实现：L5 在已有 `[Earlier summary]` 上追加新段；cache key 含「已摘要到的 history offset / 消息条数」，避免每轮全量重摘要。单测：第二轮摘要调用输入含 Earlier 块且只摘要增量片段。
- **[P2] ❌ [C:⭐⭐ I:⭐⭐⭐⭐] diff-only 上下文**
  - 推荐实现：Patcher user 优先注入当前 patch/hunk（`unified_diff` 裁到 BUDGET）+ 可选 suspect 行邻域 ±N；落点 `repair_context_blocks` / `patcher_task_builder`。单测：相对整文件注入 token 上界下降，且含关键 hunk 头。
- **[P2] ❌ [C:⭐ I:⭐⭐⭐] Context waterfall 报告**
  - 推荐实现：`_end_repair_trace` 从最近 `context_built.sections` 写 `report.json.context_waterfall`：每段 `tokens`/`pct`；CLI `--verbose` 可打 ASCII 条（或自包含 HTML，无前端依赖）。单测：fixture context_built → report 键齐全且 pct 和≈100。

---

## 3. 分层记忆

> 设计见 [DESIGN §4](bonus/DESIGN.md#4-分层记忆)。

- **[P1] ❌ [C:⭐⭐ I:⭐⭐⭐⭐] Candidate schema + 规则/LLM 双路**
  - 推荐实现：新增 `features/memory/candidate.py`：Pydantic `Candidate(topic, key, value, kind, confidence, source)`。规则抽取（stack/工具结果）+ `light_client` **仅填**规划字段；禁止自由建 topic。写 durable 前走冲突门控。hook：after_tool / after_ask。单测：非法 topic 拒绝；同 key 冲突走状态机。
- **[P1] 🔶 [C:⭐⭐ I:⭐⭐⭐⭐] 本地路径隔离**
  - 推荐实现：`resolve_under_root(workspace/.agent/memory, path)`；含 `..` 或越界抛 `MemoryPathError`。durable / episodic / dream / semantic 读前统一调用。单测：`test_memory_path_safety.py` 覆盖绝对路径与 `../`。
- **[P1] 🔶 [C:⭐⭐⭐ I:⭐⭐⭐⭐] Memory Dream 完善**
  - 推荐实现：在现有 dedup/expire/trim/durable_gc 上增加：`_rebuild_routing_table()`（依赖路由表条目）、`_suggest_promotions()`（`kind=decision` 且 hit≥N → 仅写入 trace `promotion_hints`，**默认不自动 promote**）。触发：`agent_loop` repair 结束；可选 REPL idle。`report.json.memory_health` 合并 before/after 与 Dream stats。单测：`test_memory_dream.py` 覆盖路由重建与 health 字段。
- **[P1] ❌ [C:⭐⭐ I:⭐⭐⭐⭐] `MEMORY.md` 升级为路由表**
  - 推荐实现：`MEMORY.md` 表列 `topic | entries | bytes | strategy(inline|chunked)`。小 topic 读 `topics/{t}.md`；大 topic（>阈值，如 32KB）拆 `topics/{t}/chunk-{n}.md`，召回时 semantic max-pool。`DurableMemoryStore.retrieval` 先读路由再取内容；promote 超阈值自动 split 并更新路由。单测：大 topic fixture 只读 1–2 chunk。
- **[P1] ❌ [C:⭐⭐ I:⭐⭐⭐⭐] Memory 检索质量 recall@k / precision@k**
  - 推荐实现：`tests/fixtures/memory_retrieval_labels.jsonl`（query→relevant_topics）；`scripts/eval_memory_retrieval.py` 对 semantic 与 keyword 基线算 recall@5 / precision@5；写 `eval_results/memory_retrieval.json`；可选写入 eval_report。README 摘一行数字。
- **[P2] 🔶 [C:⭐⭐ I:⭐⭐⭐] 冲突状态机**
  - 推荐实现：扩展 `ConflictResolution`：`None | Equivalent | Override | Invalid`；写 durable 前按权威序（source/角色）判定；低权威不覆盖高权威。模块：`durable.py`。单测：低权威写入被拒 / Equivalent 合并。
- **[P2] 🔶 [C:⭐⭐ I:⭐⭐⭐] 记忆 GC + episodic 上限**
  - 推荐实现：配置 `MAX_EPISODIC_NOTES`；超限按时间淘汰最旧；在 Dream 末尾统一触发；可选 durable LRU。单测：灌满后条数 ≤ 上限且保留较新。
- **[P2] ❌ [C:⭐ I:⭐⭐⭐] episodic kind 权重**
  - 推荐实现：recall 排序前按 `kind∈{error,decision,observation}` 乘可配权重，再比 cosine。默认表写入 config。单测：同相似度时 decision 排前。
- **[P2] ❌ [C:⭐⭐ I:⭐⭐⭐] episodic → durable promote**
  - 推荐实现：条件 `kind=decision` 且 `hit_count≥N`；调用 durable.write；与 Dream `promotion_hints` **同一配置面**，默认 `auto_promote=false`。单测：开关开/关行为。
- **[P2] ❌ [C:⭐ I:⭐⭐] 互斥 key 版本链**
  - 推荐实现：同 topic 语义互斥 key 保留 `history[]`（时间戳+value），读路径取最新有效项，可选追溯。单测：两次覆盖后 history 长度 2、读到最新。
- **[P2] ❌ [C:⭐ I:⭐⭐⭐] 置信度时间衰减**
  - 推荐实现：`confidence *= decay ** days_since_seen`（decay/阈值可配）；低于阈值不参与召回；Dream 与 recall 共用公式。单测：伪造旧时间戳条目被过滤。
- **[P3] ❌ [C:⭐⭐ I:⭐⭐⭐] HyDE 查询改写**
  - 推荐实现：可选 `semantic.recall_with_hyde()`：`light_client` 生成假设性答案再 embed；配置/CLI 开关默认关；与 `derive_embed_query` 规则版用同一标注集做 recall@k A/B。
- **[P3] ❌ [C:⭐ I:⭐⭐] 用户画像 schema**
  - 推荐实现：durable topic `preferences` + 轻量 pydantic

---

## 4. RAG / 检索

> 设计见 [DESIGN §4.4](bonus/DESIGN.md) · Retriever。

- **[P1] 🔶 [C:⭐⭐ I:⭐⭐⭐⭐⭐] RAG 三层统一叙事**
  - 推荐实现：①投影层 `ContextManager._get_knowledge`（episodic+durable→knowledge）②流水线 `RepairPrecedentStore` / `similar_fixes` ③`.agent/embed_cache/` content_hash。写 `docs/interview/RAG_LAYERS.md`；trace 统一：`memory_retrieval_path` · `precedent_score` · `embed_cache_hit|miss`；三层共用 `derive_embed_query()` 并单测 query 一致。report 可聚合 `embed_cache_hit_rate`。
- **[P1] 🔶 [C:⭐⭐⭐ I:⭐⭐⭐⭐⭐] Retriever Agentic RAG**
  - 推荐实现：强化 Retriever system prompt（stack 关键词 → grep → read/ast 多跳）；每次 tool 后向 `node_timings.retrieval_steps` append `{tool, args, hits}`；emit `retrieval_step`；保留 `retrieval_path=llm|rule|degrade`。规则路径仅作降级。eval：相关 case 断言 steps 含 `grep`。
- **[P2] ❌ [C:⭐⭐ I:⭐⭐⭐⭐] Skill 向量 RAG（N>100）**
  - 推荐实现：`SkillCatalog.embed_index` 复用 `semantic.py`；`match_skill`：向量 top-k → pattern/regex 精筛；N 小时仍走现有 regex。fixtures 可造 ~100 synthetic skill 压测（gitignore 或测试专用）。单测：命中排序与 regex 回退。

---

## 5. Multi-Agent 编排

> 设计见 [DESIGN §12](bonus/DESIGN.md#12-multi-agent-编排)。

- **[P1] 🔶 [C:⭐⭐ I:⭐⭐⭐⭐] 动态 Agent 裁剪**
  - 推荐实现：`repair_factory` 导出 `_SIMPLE_ISSUE_TYPES`（如 import/syntax）；`orchestrator.repair()` 在 `_parse_issue` 后若 simple 且已有 retriever，则 `self.retriever = None`（或 skip 标记，pipeline 已支持 None）。可选 `wire_orchestrator(issue_type=...)`。单测：`test_repair_factory` / `test_orchestrator` 断言 simple 路径无 retriever `agent_asks`。
- **[P1] ❌ [C:⭐⭐⭐ I:⭐⭐⭐⭐⭐] composite subtasks 编排**
  - 推荐实现：composite 规则生成 ≥2 `RepairSubTask`；`pipeline` 提 `_run_subtask_cycle(state, subtask)`：缩窄 `suspect_files` 后 localize→patch→verify；patches 按 `depends_on` 拓扑合并；Blackboard key 前缀 `subtask:{id}:`；trace `subtask_started` / `subtask_done`。测 case_010（可先 fake）。**Planner 独立 Agent 后置。**
- **[P1] ❌ [C:⭐⭐⭐ I:⭐⭐⭐⭐⭐] Planner Agent + 可解释 RepairPlan**
  - 推荐实现：`create_planner` + 单次 JSON `complete` → `RepairPlan(reasoning, subtasks, suspect_files, language)`；**只规划不调 tool**；失败回落规则 `_parse_issue`；trace `planner_invoked` · `fallback=rule|llm` · `plan_rationale`。前置：上条规则 subtasks 稳定。eval：composite 对比 rule vs planner（可选）。
- **[P1] 🔶 [C:⭐⭐ I:⭐⭐⭐] 分 Agent 配额加强**
  - 推荐实现：审计四角色各持独立 `ToolQuota`；`repair_factory` 注入角色配额表（Localizer read 紧、Patcher write 松）。单测：`test_quota.py` Localizer read 耗尽不影响 Patcher write。
- **[P2] 🔶 [C:⭐ I:⭐⭐⭐] Localizer∥Retriever 去重**
  - 推荐实现：`blackboard_merge` 合并 suspects 时按 `(file_path, line)` 去重；冲突保留 **localizer**。单测：`test_blackboard_merge.py`。

---

## 6. Eval / 消融 / 可观测

> 设计见 [DESIGN §19–§20](bonus/DESIGN.md)。

### 6.1 指标 · report · replay

- **[P1] ✅不足 [C:⭐⭐ I:⭐⭐⭐⭐] 统一 token 会计 cache_hit_rate**
  - 推荐实现：`token_accounting` 从 `context_built` / provider metadata 汇总 `cache_read_tokens`、`cache_hit_rate`；`run_trace._end_repair_trace` 写入 `report.json.token_usage`。单测：`test_token_accounting` 断言字段存在。
- **[P1] 🔶 [C:⭐⭐ I:⭐⭐⭐⭐] runtime_metrics 统一**
  - 推荐实现：定义 `report.runtime_metrics`：`cache_hit_rate` · `parse_retry_count` · `retry_count` · `tool_steps` · `writes_used/limit` · `shell_used/limit`；`agent_loop`/`pipeline` 收尾写入；与 token 会计同一 PR。可选 Prometheus gauge 同源。单测：orch 结束读 report 键齐全。
- **[P1] 🔶 [C:⭐⭐ I:⭐⭐⭐⭐] 分 Agent token/latency 表**
  - 推荐实现：聚合 trace `agent_asks` → `by_agent[role]={tokens, ms, calls}`，挂同一 report 段。扩展 `test_l2_state_binding`。
- **[P1] ❌ [C:⭐⭐ I:⭐⭐⭐⭐⭐] 消融矩阵**
  - 推荐实现：跑 `cli ablation`（full/single/no_retriever）；README 固化表（fix_rate/precision/retries）；可选 `scripts/regression_check.py` 对比 `ci_baseline_report.json` 阈值。面试附件可放 `docs/interview/ABLATION.md`。
- **[P1] ❌ [C:⭐⭐ I:⭐⭐⭐⭐⭐] trace replay / prompt 调试**
  - 推荐实现：`agent_runtime.cli replay <run_id>` **只读** `.agent/runs/<id>/trace.jsonl`：树状摘要；`--step N --show-prompt` 从 `context_built`/`prompt_preview` 还原；可选 `--diff stepA stepB`。不重调模型。单测：fixture trace → 输出格式快照。
- **[P2] ❌ [C:⭐ I:⭐⭐⭐⭐] 单次 repair 成本模型**
  - 推荐实现：`pricing_table.yaml`（model→单价）；`token_accounting.estimate_cost(report)`；ablation/report 增 `estimated_cost_usd` 列。单测：固定 token 数得到固定费用。
- **[P2] ❌ [C:⭐ I:⭐⭐⭐⭐] LLM 调用预算硬顶**
  - 推荐实现：配置 `max_llm_calls_per_repair`；每次 `complete*` 计数；超阈 `stop_reason=budget_exhausted` 并写入 runtime_metrics。单测：FakeClient 调用次数封顶。
- **[P2] ❌ [C:⭐ I:⭐⭐⭐] 统一错误码 taxonomy**
  - 推荐实现：枚举 `FixLoopErrorCode`（如 `GATE5_DUPLICATE`、`PHASE_TIMEOUT`、`SEMANTIC_DRIFT`、`CONTEXT_OVERFLOW`）；L1/L2 emit 统一 `error_code` 字段；文档映射表。单测：代表性路径 payload 含枚举值。

### 6.2 Case · 消融 · 测评质量

- **[P1] 🔶 [C:⭐ I:⭐⭐⭐] patch_equivalence 进 eval_report**
  - 推荐实现：`CaseResult.equivalence ∈ {full, partial, none}`；run 结束对 actual vs `expected_patch.diff` 调 `patch_utils.patch_equivalence`；`build_eval_report` 增 `equivalence_by_type` 与 `avg_equivalence_full_rate`。单测：metrics + patch_engine。
- **[P1] 🔶 [C:⭐⭐ I:⭐⭐⭐] Case 016–020 补全**
  - 推荐实现：按错误类型补 016 logic … 020 composite-lite；每案 `issue.txt` / `expected_patch.diff` / `metadata.yaml` / `min_lines.txt`；先保证 fake eval 可跑。
- **[P1] ❌ [C:⭐⭐ I:⭐⭐⭐⭐⭐] Naive 单轮基线对照**
  - 推荐实现：`eval/variants.py` 增加 `naive`：单次 `complete`、无 tool schema、无 Docker verify（或轻量 apply）；ablation 第四列；`docs/interview/NAIVE_BASELINE.md` 失败模式分类（wrong_file / no_patch / regression）。
- **[P1] ❌ [C:⭐⭐ I:⭐⭐⭐⭐] Golden trace 回归**
  - 推荐实现：fixture 锁定关键事件名序列（如 `react_phase`、`agent_asks`、`context_built`）；CI 对精简后的 event list 做 golden diff（避免全文脆弱 snapshot）。
- **[P2] ❌ [C:⭐ I:⭐⭐⭐] badcase → eval Case 晋升**
  - 推荐实现：`scripts/promote_badcase.py` 读 `.agent/runs/.../badcase.json` → 生成 `case_XXX/` 骨架；人工 review 后入 eval。单测：fixture 目录快照。
- **[P2] 🔶 [C:⭐ I:⭐⭐⭐] 意图对抗 `case_adv_ambiguous`**
  - 推荐实现：模糊 issue；`metadata.expected_status=exhausted`；eval 断言非虚假 success。可与负样本 exhausted case 共用模式。
- **[P2] ❌ [C:⭐⭐ I:⭐⭐⭐] 并行 eval runner**
  - 推荐实现：`eval --jobs N` 用 `ProcessPoolExecutor` 按 case 并行；文档注明 API 限流/独立 cwd。单测：jobs=2 冒烟（fake）。
- **[P3] ❌ [C:⭐ I:⭐⭐] 难度重标定脚本**
  - 推荐实现：`relabel_case_difficulty.py` 读历史 eval 统计 → 写回 `metadata.difficulty`（dry-run 默认）。
- **[P3] ❌ [C:⭐⭐ I:⭐⭐] 多语言 Case**
  - 推荐实现：java/node 各加 1 最小 case

---

## 7. CLI · 演示 · 文档

- **[P2] 🔶 [C:⭐⭐ I:⭐⭐⭐] streaming REPL**
  - 推荐实现：REPL 路径使用 `complete_stream` + `CLIProgressCallback.on_chunk` 刷屏；与 §1 流式 cancel 共用同一 `cancel_token`。
- **[P3] ❌ [C:⭐ I:⭐⭐] workspace 切换检测**
  - 推荐实现：`WorkspaceContext` 记录 `cwd`/`root_hash`；变更时 invalidate prefix hash + clear working `recent_files`。单测：模拟 cwd 切换后 prefix 重算。
- **[P3] 🔶 [C:⭐ I:⭐⭐] workspace fingerprint 文档化**
  - 推荐实现：README 或 DESIGN 一节说明 content-hash 与 prefix cache 绑定（代码已有则只补文档，不改哈希算法）。
- **[P3] ❌ [C:⭐⭐ I:⭐⭐⭐] 歧义 LLM fallback**
  - 推荐实现：`_parse_issue` 得到 `unknown` 时，`light_client` **一次** JSON 分类 `issue_type`；解析失败或超时保持 `unknown`，不阻塞主路径。单测：unknown→分类成功 / 失败保持。

---

## 8. 安全 · 沙箱 · 工具

> 设计见 [DESIGN §6 / §9 / §16 / §18](bonus/DESIGN.md)。

- **[P1] ❌ [C:⭐⭐ I:⭐⭐⭐⭐] 逃逸回归 Case**
  - 推荐实现：新增 `case_adv_sandbox_001`（试读 `/etc/passwd`、curl）；`test_sandbox_escape.py` 用 fake docker 断言命令被拒、repo 干净。文档链 DESIGN §16.5。
- **[P2] ❌ [C:⭐ I:⭐⭐⭐] registry ↔ auto_schema CI**
  - 推荐实现：`scripts/check_tool_schema_sync.py` 对比 `src/tools/registry.py` 与 `agent_runtime/tools.py` 工具名集合；CI/pre-commit 非零退出并打印差集。
- **[P2] 🔶 [C:⭐⭐ I:⭐⭐⭐] run_shell 白名单扩展**
  - 推荐实现：扩展 `shell_security` 允许列表；与 §18 敏感文件名策略合并文档；非法命令拒 + trace 字段。单测：白名单内外各一命令。
- **[P2] 🔶 [C:⭐ I:⭐⭐] grep 相邻行合并**
  - 推荐实现：`tools._format_grep` 后处理：同文件连续行号合并为块，减 tool 结果 token。单测：连续命中合并为一段。
- **[P2] ❌ [C:⭐ I:⭐⭐⭐] `FIXLOOP_MAX_SANDBOXES`**
  - 推荐实现：`sandbox_manager` 模块级 `Semaphore`（env `FIXLOOP_MAX_SANDBOXES`）；获取失败时排队或返回明确错误。单测：并发上限。
- **[P2] 🔶 [C:⭐ I:⭐⭐⭐] `execution_tier=container` metadata**
  - 推荐实现：`sandbox_verify` / `sandbox_tools` 结果 metadata 写 `execution_tier=container|host`，进入 trace/report。单测：container 路径字段为 container。
- **[P2] 🔶 [C:⭐⭐ I:⭐⭐⭐] 敏感产物加密接线**
  - 推荐实现：若设 `FIXLOOP_ENCRYPT_KEY`，`run_store` 写 issue/patch 时调已有 `crypto_utils.encrypt()`；读路径解密；无 key 保持明文。文档 env。单测：有 key 时落盘非明文、可读回。

---

## 9. Checkpoint · 续跑 · Session

> 设计见 [DESIGN §11](bonus/DESIGN.md#11-checkpoint)。

- **[P1] 🔶 [C:⭐⭐ I:⭐⭐⭐⭐] L2 `--resume-repair` 真续跑**
  - 推荐实现：新建 `src/repair/checkpoint_load.py`：`load_repair_checkpoint(repo, run_id) -> RepairState | None`，恢复 `retry_count`/`phase`/`feedback`/`suspect_locations`/`blackboard_snapshot`。`orch.repair(..., resume_run_id=)` 成功则跳过 parse/localize，从 patch 循环重入（**不恢复 L1 agent session**）。CLI `args.resume_repair` 传入。单测：写盘→load→mock patcher 验证重入。
- **[P1] ✅弱 [C:⭐ I:⭐⭐⭐⭐] Checkpoint 触发点规范**
  - 推荐实现：`CheckpointTrigger = Literal["step_end","user_cancel","ask_end"]`；`create_checkpoint` 校验；payload 含 `last_tool`，cancel 时含 `in_flight_tool`；文档 `docs/interview/CHECKPOINT.md`。`--resume` / load 仅当 `trigger=step_end` 允许 mid-loop。单测：三 trigger 字段；非法 trigger 拒写。
- **[P2] ❌ [C:⭐ I:⭐⭐] SessionStore `.bak`**
  - 推荐实现：`persistence.py` 写 session 前 `copy → .bak`；`load` 主文件失败则尝试 bak。单测：损坏主文件回退成功。
- **[P2] ❌ [C:⭐⭐ I:⭐⭐⭐] REPL `/save` `/load`**
  - 推荐实现：分期只做 JSON 会话到 `.agent/sessions/<name>.json`（含 todos/history 指针）；不做全量 `/sessions` IDE 级产品。单测：save→load 字段往返。

---

## 10. Patch · JSON · 输出

> 设计见 [DESIGN §14 / §17](bonus/DESIGN.md)。

- **[P1] 🔶 [C:⭐⭐ I:⭐⭐⭐⭐] AST 语义等价进主路径**
  - 推荐实现：Patcher apply 后、pytest 前，对每个目标文件 old/new 调用既有 `check_semantic_equivalence`（签名级）；`drift` → `state.agent_errors["semantic_drift"]`、跳过 verify、直接 retry；trace `semantic_check{status, detail}`。单测：构造删除无关函数的 patch → 拒绝。
- **[P2] 🔶 [C:⭐ I:⭐⭐] 多级 parse 补 json5**
  - 推荐实现：`output_parsers` strict JSON 失败后走 json5 或轻量 trailing-comma 修复（避免重依赖更佳）；单测：trailing comma / 注释类可修复用例。
- **[P2] 🔶 [C:⭐ I:⭐⭐⭐] Provider JSON mode 全角色**
  - 推荐实现：`AgentConfig.response_format` 表驱动；`src/agents/factory.py` 为四 L2 角色设默认 JSON mode（provider 支持时）；不支持则降级文本 + 现有多级 parse。单测：factory 产出的 config 字段符合表。
- **[P2] ❌ [C:⭐⭐ I:⭐⭐⭐] Property-based JSON fuzz**
  - 推荐实现：`hypothesis` 生成畸形串喂 `output_parsers`；断言进程不崩溃且返回结构化 `validation_errors`。dev 依赖 hypothesis；`tests/test_output_parsers_property.py`。

---

## 不建议为面试专门做

| 条目 | 原因 |
|------|------|
| workspace 切换检测 · 用户画像 | 故事弱 / 产品向 |
| 多语言 Case 全量 · SWE-bench | 成本高、易穿帮 |
| 全量 `/sessions` 迁移 | 与 repair 主线无关 |
| Web / 多租户 / SSE | 见 OUT_OF_SCOPE |

---

## 附录 · 执行顺序建议

```text
§5/§1/§10 接线 → §6 可观测 + §5 subtasks → 按主题补 P2/P3
```

| 批次 | 建议条目 |
|------|----------|
| 1 | 死循环检测 · AST 语义等价 · patch_equivalence · 动态裁剪 |
| 2 | resume-repair（最小续跑） |
| 3 | token / runtime_metrics / by_agent |
| 4 | composite subtasks（规则编排） |
| 5 | Naive · 消融矩阵 · RAG 叙事（§4/§6 选 2） |

动手前：按上列「推荐实现」写 plan（`writing-plans`）→ TDD → PR；大条目可落 `docs/superpowers/specs/`。
