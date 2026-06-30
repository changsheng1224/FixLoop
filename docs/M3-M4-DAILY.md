# M3-M4 每日开发计划（Week 5-8）

> 每天约 4-6 小时有效编码时间。⚡ 核心任务必须完成，🔧 辅助任务可弹性处理。Day 编号接续 M1-M2。

---

## M3：记忆系统 + 持久化 + 会话恢复 + 安全 + 对话摘要（Week 5-6）

**目标：让 Agent 从"每次调用都是全新的"变成"有记忆、可恢复、有审计"。**

---

### Day 11（周一）：Working Memory + Episodic Memory

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:00 | ⚡ 实现 `features/memory.py`：`default_memory_state()` 返回初始结构 `{working: {task_summary:"", recent_files:[]}, episodic_notes:[], file_summaries:{}, next_note_index:0}`。`normalize_memory_state(state, workspace_root)` — 兼容旧格式、裁剪超限条目 | 空状态入参 → 规范化输出不变 |
| 10:00-11:30 | ⚡ 实现 Working Memory 层：`set_task_summary(state, user_message)` → 截断到 300 字。`remember_file(state, path)` → dedupe + append + trim 到 8 个。`set_file_summary(state, path, summary)` → 存储 `{summary, created_at, freshness}`。`invalidate_file_summary(state, path)` → 删除 | 连续记录 10 个文件 → 列表只保留最后 8 个 |
| 11:30-12:00 | 🔧 Working Memory 单测：task_summary、recent_files 去重与截断、file_summary 增删 | `tests/test_memory.py` 4 tests green |
| 14:00-15:30 | ⚡ 实现 Episodic Memory 层：`append_note(state, text, tags, source, kind)` → 生成 `{text, tags, source, created_at, note_index, kind}` → dedupe by text → trim 到 12 条。`retrieval_candidates(state, query, limit=3)` → tokenize query → 匹配 tags（精确）+ keywords（重叠）+ recency（时间衰减）排序取 top_k | 查询 "test" 命中 tag 含 "test" 或文本含 "pytest" 的笔记 |
| 15:30-16:30 | ⚡ 实现 Agent 中的记忆钩子：`update_memory_after_tool(name, args, result)` — `read_file` 后调 `remember_file` + `set_file_summary`（摘要取前 180 字）；`write_file`/`patch_file` 后调 `remember_file` + `invalidate_file_summary`；`run_shell` 失败后调 `append_note` 记录 process note。`record_process_note_for_tool(name, metadata)` — status 为 partial_success/error/rejected 时记录 | 一轮 ask 后 session.memory 中 recent_files 和 notes 正确更新 |
| 16:30-17:30 | 🔧 Episodic 单测 + 记忆钩子集成测试 | `tests/test_memory.py` +3 tests；`tests/test_memory_hooks.py` 2 tests green |

**Day 11 验收：** `agent.update_memory_after_tool("read_file", {"path":"a.py"}, result)` 后 `memory.recent_files` 含 `a.py` 且 `file_summaries["a.py"]` 有摘要。连续操作后列表不超限。

---

### Day 12（周二）：Durable Memory + TaskState + Session 持久化

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 实现 Durable Memory：`DurableMemoryStore(root)` — 读写 `.agent/memory/MEMORY.md`（索引文件）+ `topics/{topic}.md`（主题笔记文件）。4 种内置主题：`project-conventions` / `key-decisions` / `dependency-facts` / `user-preferences`。`promote(promotions: list[(topic, text)])` → 按 topic 分类写入文件，同 subject 自动替换旧条目 | `remember("Preference: use pytest")` → `.agent/memory/topics/user-preferences.md` 新增一行 |
| 10:30-11:30 | ⚡ 实现 `promote_durable_memory(user_message, final_answer)`：检测 user_message 中是否含 "remember"/"记住"/"保存" 等意图词；如果含，从 final_answer 中按 `Project convention:` / `Decision:` / `Dependency:` / `Preference:` 行前缀提取笔记，经 `reject_durable_reason()` 过滤（空内容/含密钥/重复 checkpoint 信息/超长噪音）后 promotion | `ask("remember the default test runner is pytest")` → final_answer 含 `Preference: default test runner is pytest` → 自动写入 durable memory |
| 11:30-12:00 | 🔧 Durable Memory 单测：promote、retrieval、同主题替换、索引读取 | `tests/test_durable_memory.py` 3 tests green |
| 14:00-15:00 | ⚡ 实现 `task_state.py`：`TaskState(run_id, task_id, user_request, status, tool_steps, attempts, last_tool, stop_reason, final_answer, checkpoint_id, resume_status)` dataclass。类方法 `create(task_id, user_request)` → 自动生成 run_id。方法 `record_attempt()`, `record_tool(name)`, `stop(reason, status)`, `stop_step_limit()`, `stop_retry_limit()`, `finish_success(final_answer)`。`to_dict()` / `from_dict(data)` 往返 | TaskState 状态机：running → completed/stopped/failed |
| 15:00-16:00 | ⚡ 实现 `session_store.py`：`SessionStore(root)` — `save(session)` 写入 `.agent/sessions/{id}.json`。`load(session_id)` 读取 JSON。`latest()` 返回最近修改的 session id（按 mtime 排序） | session 保存后重启进程可恢复 |
| 16:00-17:00 | ⚡ 实现 `run_store.py`：`RunStore(root)` — `start_run(task_state)` 创建 `.agent/runs/{run_id}/` 目录。`write_task_state(task_state)` 写入 `task_state.json`（原子写：先写 .tmp 再 replace）。`append_trace(task_state, event)` JSONL 逐行追加到 `trace.jsonl`。`write_report(task_state, report)` 写入 `report.json`（原子写） | 中途 kill 进程 → 没有半截 JSON |
| 17:00-17:30 | 🔧 task_state + session_store + run_store 单测 | `tests/test_persistence.py` 3 tests green |

**Day 12 验收：** Durable memory 正确读写文件。TaskState 状态机完整。Session 保存后 `--resume latest` 可恢复。Run 工件原子写不产生半截文件。

---

### Day 13（周三）：Checkpoint 跨轮恢复 + 安全脱敏

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 实现 `checkpoint.py`：`CHECKPOINT_SCHEMA_VERSION = "1.0"`。`RUNTIME_IDENTITY_KEYS` 定义哪些字段组成 runtime 身份（cwd/model/approval/max_steps/tools 签名等）。`current_runtime_identity(agent)` 返回当前身份快照。`create_checkpoint(agent, task_state, user_message, trigger)` → 记录 current_goal/blocker/next_step/key_files（含 freshness hash）/runtime_identity → 写入 session.checkpoints | 每次 ask 结束自动创建 checkpoint |
| 10:30-11:30 | ⚡ 实现 `evaluate_resume_state(agent)`：读取 session 中的 checkpoint → 检查 schema_version 是否匹配 → 遍历 key_files 对比当前文件 freshness → 对比 current_runtime_identity 与保存的 runtime_identity → 返回 status：`no-checkpoint` / `full-valid` / `partial-stale`（文件变化）/ `workspace-mismatch`（身份变化）/ `schema-mismatch` | Resume 时自动检测 offline 期间的文件改动 |
| 11:30-12:00 | 🔧 checkpoint 单测：正常恢复、文件 stale 检测、runtime 身份变化检测 | `tests/test_checkpoint.py` 3 tests green |
| 14:00-15:00 | ⚡ 扩展 `security.py`（Day 6 基础上补充）：`detected_secret_env_items(secret_env_names)` 扫描 `os.environ` 中所有疑似敏感变量（含 `API_KEY`/`TOKEN`/`SECRET`/`PASSWORD` marker）。`redact_artifact(value, key, secret_env_names)` 递归处理 dict/list/str，敏感 key 的值替换为 `<redacted>` | `redact_artifact({"DEEPSEEK_API_KEY":"sk-xxx"}, "DEEPSEEK_API_KEY")` → `<redacted>` |
| 15:00-16:00 | ⚡ 将安全脱敏集成到 Agent 的输出链路：`emit_trace()` 中调 `redact_artifact(payload)`。`build_report()` 中调 `redact_artifact(report)`。`run_shell()` 使用 `shell_env(allowlist)` 过滤环境变量 | trace.jsonl 和 report.json 中 API key 不可见 |
| 16:00-17:00 | ⚡ Agent 中补齐 `from_session()` 类方法：`cls.from_session(model_client, workspace, session_store, session_id, **kwargs)` 读取 session → 恢复 history/memory/checkpoint → 创建 Agent 实例。CLI `--resume latest` 实现 | `python -m agent_runtime --resume latest` 恢复上次会话 |
| 17:00-17:30 | 🔧 安全集成测试 + resume 集成测试 | `tests/test_resume.py` 2 tests green |

**Day 13 验收：** 一次 ask 后离线修改文件 → resume 时标记 partial-stale。Trace 和 report 中无 API key。`--resume latest` 可用。

---

### Day 14（周四）：对话摘要 + AgentLoop 中集成 TaskState

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 实现对话自动摘要：`context_manager.py` 中增加 `_maybe_summarize_history(history, trigger_tokens=2600)` — 当 history token 数超过阈值时，取前一半历史（old_history）调用 `model_client.complete("Summarize the following conversation...", max_tokens=300)` 生成一句摘要，返回 `[{"role":"system","content":"[Earlier summary]: ..."}, *recent_history]` | 30 轮模拟对话历史 → 自动触发摘要 → prompt 总 token 回到预算内 |
| 10:30-11:30 | ⚡ 摘要质量保障：① 摘要长度限制在 200 tokens ② 摘要中包含关键实体（文件名、错误类型、已尝试的操作） ③ 如果模型生成摘要失败（返回空），退化为简单裁剪（保留最近 8 条） | 降级方案生效 |
| 11:30-12:00 | 🔧 对话摘要单测（用 FakeClient 提供摘要） | `tests/test_summarization.py` 2 tests green |
| 14:00-15:30 | ⚡ 将 TaskState 集成到 AgentLoop：Day 4 的 AgentLoop 是简化版，现在补全——`run()` 开始时创建 `TaskState` → 记录 `run_started` trace 事件 → 每轮调用前 `record_attempt()` → tool 执行后 `record_tool()` → final 时 `finish_success()` → 停机时 `stop_step_limit()` / `stop_retry_limit()` → 结束时 `write_report()` + `create_checkpoint()` | 每次 ask 产生完整的 task_state.json + trace.jsonl + report.json |
| 15:30-16:30 | ⚡ 完善 `Agent.emit_trace(task_state, event, payload)` — 自动注入 `event` 和 `created_at`，经 `redact_artifact` 后追加到 trace.jsonl。trace 事件类型：`run_started` / `prompt_built` / `model_requested` / `model_parsed` / `tool_executed` / `checkpoint_created` / `run_finished` | trace.jsonl 可完整复盘单次 ask 的每一步 |
| 16:30-17:30 | 🔧 TaskState 集成测试 + Trace 完整性测试 | `tests/test_task_state.py` + `tests/test_trace.py` 共 3 tests green |

**Day 14 验收：** 对话摘要自动触发且内容可读。每次 ask 产生完整的 task_state + trace + report。Trace 包含 7 种事件类型。

---

### Day 15（周五）：M3 收尾 + 真实 API 全流程测试

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:00 | ⚡ 真实 API 测试记忆系统：用 DeepSeek API 跑 3 轮对话，每轮验证 memory 累积是否正确——第 1 轮 `read_file("a.py")` → recent_files 含 a.py；第 2 轮 `write_file("b.py")` → a.py 仍在但 b.py 加入；第 3 轮 "what files did I read?" → Agent 应从 memory 给出答案而不是重新搜 | 记忆跨轮生效 |
| 10:00-11:00 | ⚡ 真实 API 测试 resume：跑 1 轮对话 → 退出 → `--resume latest` → 验证 history 和 memory 恢复。修改一个文件 → resume → 验证 Agent 收到 partial-stale 警告 | Resume 全流程正确 |
| 11:00-12:00 | ⚡ 真实 API 测试对话摘要：构造超长对话（约 8-10 轮），触发摘要生成，验证摘要后的轮次 Agent 仍能引用早期上下文 | 摘要未丢失关键信息 |
| 12:00-12:30 | 🔧 修复真实 API 测试中发现的问题 | Prompt / 逻辑修复 |
| 14:00-15:30 | ⚡ 补充 M3 的单测覆盖：memory 完整单测（working + episodic + durable 全路径）、checkpoint 边界情况（空 checkpoint、schema 升级）、安全脱敏边界（嵌套 dict、空值） | M1-M3 目标 50+ tests green |
| 15:30-16:30 | ⚡ 代码整理 + `ruff check` + `ruff format`。补全 docstring | CI 风格零 warning |
| 16:30-17:30 | ⚡ M3 复盘 + git tag m3-done。统计代码量 | M3 正式完成 |

**Day 15 验收（M3 里程碑）：**
- [ ] 三层记忆全部生效，跨轮累积正确
- [ ] Durable memory 写入 `.agent/memory/` 人类可读
- [ ] `--resume latest` 可用，stale 检测正常
- [ ] 对话摘要自动触发，降级方案可用
- [ ] 每次 ask 产出 task_state + trace + report
- [ ] Trace 和 report 中无敏感信息
- [ ] `pytest tests/ -v` 全绿（50+ tests）
- [ ] 代码量约 1500 行（M1 500 + M2 500 + M3 500）

---

### M3 周末缓冲

- 周六上午：补未完成的单测
- 周六下午：Durable memory 的 topic 文件格式美化
- 周日：休息

---

## M4：语义记忆 + 工具配额 + Circuit Breaker + Deterministic Replay（Week 7-8）

**目标：在 M3 可靠 Agent 基础上加入高级能力——语义检索、执行配额、API 熔断、行为回放。**

---

### Day 16（周一）：Semantic Memory 语义检索

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:00 | ⚡ `pip install sentence-transformers`。实现 `features/memory.py` 中新增 `SemanticMemory` 类：`__init__(model_name="all-MiniLM-L6-v2")` → 首次加载模型（约 80MB）。`encode(text)` → `self.model.encode(text)` 返回 numpy array。`add(note)` → 将 note 的 text + embedding 存入内存列表。`search(query, top_k=3)` → encode query → cosine_similarity 与所有已存 embedding 计算 → 排序取 top_k | `add("pytest fixture setup")` → `search("test initialization")` 命中 |
| 10:00-11:30 | ⚡ 将 SemanticMemory 集成到 LayeredMemory：`retrieval_candidates()` 中 keywords 匹配优先快速筛选，semantic 匹配作为补充（处理同义词和英文变体）。两路结果合并 dedupe 后取 top_k。保留纯 keywords 路径作为 semantic 模型加载失败时的降级 | 卸载 sentence-transformers 后 → 退化为纯 keywords 检索，不报错 |
| 11:30-12:00 | 🔧 Semantic Memory 单测：语义命中同义词、keywords 命中精确、降级测试 | `tests/test_semantic_memory.py` 3 tests green |
| 14:00-15:30 | ⚡ 实现 Ollama 本地模型客户端：`providers/clients.py` 新增 `OllamaModelClient(model, host, temperature, top_p, timeout)`。`complete(prompt, max_tokens)` → `urllib.request.Request(host+"/api/generate", data={model, prompt, stream:False, options:{num_predict:max_tokens, temperature, top_p}})` → 解析 `response` 字段 | `python -m agent_runtime --provider ollama --model qwen3.5:4b "hello"` 成功返回 |
| 15:30-16:30 | ⚡ 实现 OpenAI 兼容客户端：`providers/clients.py` 新增 `OpenAICompatibleModelClient(model, base_url, api_key, temperature, timeout)`。`complete(prompt, max_tokens)` → POST `/v1/responses` → `{model, input:[{role:"user", content:[{type:"input_text", text:prompt}]}], max_output_tokens}`。支持 SSE 流解析（`text/event-stream` content-type），提取 usage/cached_tokens | `python -m agent_runtime --provider openai "hello"` 成功返回 |
| 16:30-17:30 | 🔧 Ollama + OpenAI 客户端单测（至少各 1 个 Fake HTTP server 测试） | `tests/test_providers.py` 2 tests green |

**Day 16 验收：** 语义检索可命中同义词。Ollama 和 OpenAI 客户端可正常收发。现在共 4 种 Provider（Anthropic/Ollama/OpenAI/Fake）。

---

### Day 17（周二）：工具配额 + 进度回调

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 实现 `tool_executor.py` 中新增 `QuotaEnforcer` 类：`__init__(max_writes=20, max_shell=10, max_total=50)`。`check(tool_name)` → 判断对应计数器是否超限，超限返回 False。`record(tool_name)` → 计数器 +1。`status()` → 返回 `{writes_used, shells_used, total_used, limits}` | 连续 21 次 write_file → 第 21 次返回 "quota exceeded: max 20 writes" |
| 10:30-11:30 | ⚡ 将 QuotaEnforcer 集成到 ToolExecutor：闸口顺序中增加"配额检查"（在第 4 步重复检测之后）。`/session` REPL 命令显示当前配额使用情况 | REPL 中 `/session` 可见剩余配额 |
| 11:30-12:00 | 🔧 Quota 单测：各类型配额、超限返回、状态查询 | `tests/test_quota.py` 3 tests green |
| 14:00-15:00 | ⚡ 实现 `callbacks.py`：`ProgressCallback` Protocol 定义 `on_step_start(step, max_steps)`, `on_tool_executed(name, result_preview)`, `on_final_answer(text)`。`CLIProgressCallback` 实现：每步开始打印 `[{step}/{max_steps}]`，工具执行后打印 `✅ {name} ({chars} chars)` 或 `❌ {name} ({error})` | REPL 模式看到清晰的进度指示 |
| 15:00-16:00 | ⚡ AgentLoop 中集成进度回调：`run(user_message, callback=None)` → 每轮循环调用对应 callback 方法。CLI 创建 `CLIProgressCallback()` 传入 | `python -m agent_runtime "explain this repo"` → 实时看到 `[1/6] read_file... ✅` `[2/6] search... ✅` |
| 16:00-17:00 | ⚡ 工具降级链实现：`tools.py` 中 `search` 工具：优先 `shutil.which("rg")` → 不行 fallback Python grep。`run_shell` 增加降级逻辑预留（Docker 将来 fallback subprocess）。降级时 `append_note(f"{tool} downgraded: {reason}")` | 卸载 rg → search 仍能返回结果，日志记录降级原因 |
| 17:00-17:30 | 🔧 回调 + 降级单测 | `tests/test_callbacks.py` + `tests/test_fallback.py` 3 tests green |

**Day 17 验收：** 配额正确限制工具调用。REPL 有清晰进度指示。rg 不可用时 search 自动降级。

---

### Day 18（周三）：Circuit Breaker + API 熔断

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 实现 `providers/circuit_breaker.py`：`CircuitBreaker(failure_threshold=5, recovery_timeout=30)` 类。状态机三态：`CLOSED`（正常）→ 连续失败 ≥ threshold → `OPEN`（拒绝请求）→ 等待 recovery_timeout 秒 → `HALF_OPEN`（允许 1 次探测）→ 成功则回 CLOSED，失败则回 OPEN。`call(fn, *args, **kwargs)` 方法包裹执行 | 模拟连续返回 HTTP 500 → 第 5 次后电路打开 → 后续请求立即 raise → 30s 后半开探测 |
| 10:30-11:30 | ⚡ 将 CircuitBreaker 包裹所有模型客户端的 `complete()` 调用：在 Agent 中创建 `self.cb = CircuitBreaker()`，`agent._call_model(prompt, max_tokens)` 中调用 `self.cb.call(lambda: self.model_client.complete(...))`。熔断时返回特定错误给 AgentLoop，AgentLoop 判断为不可恢复错误 → 终止当前 ask | 模型 API 连续失败 → Agent 优雅终止而非卡死 |
| 11:30-12:00 | 🔧 CircuitBreaker 单测：正常调用、熔断触发、半开探测成功、半开探测失败回 OPEN、超时恢复 | `tests/test_circuit_breaker.py` 5 tests green |
| 14:00-15:30 | ⚡ 实现 `replay.py`：`ReplayRunner` 类。`__init__(trace_path)` → 读取 trace.jsonl 所有事件。`replay(agent)` → 遍历事件，对 `tool_executed` 事件用相同 name+args 重新执行工具，对比实际 result 与 trace 中的 `result` 字段。输出 `ReplayResult(matches: int, diffs: list[{tool, expected, actual}])` | 一次真实运行后 replay → 100% 匹配（文件未变）。文件已变 → diffs 列表正确标注差异 |
| 15:30-16:30 | ⚡ CLI 增加 `--replay <trace_path>` 命令：从 trace 回放并打印 diff 报告 | `python -m agent_runtime --replay .agent/runs/run_xxx/trace.jsonl` 输出匹配/差异统计 |
| 16:30-17:30 | 🔧 Replay 单测：全部匹配、部分差异、trace 格式异常处理 | `tests/test_replay.py` 3 tests green |

**Day 18 验收：** CircuitBreaker 状态机正确。Replay 可回放并检测差异。API 不可用时 Agent 不卡死。

---

### Day 19（周四）：系统集成 + 完善 Agent 输出管道

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ Agent 中集成 M4 所有新模块：`__init__` 中初始化 `SemanticMemory`（可选，try/except 降级）、`QuotaEnforcer`、`CircuitBreaker`。`ask()` 中接上所有中间件。确保 M1-M4 所有功能协同工作 | Agent 初始化不报错，所有模块就位 |
| 10:30-12:00 | ⚡ 完善 `build_report(task_state)`：汇总 prompt_metadata（各 section 大小、cache 命中）、completion_metadata（usage tokens）、durable_promotions/rejections、node_timings（各阶段耗时）、secret_env_summary。report.json 作为单次 ask 的完整摘要 | report.json 包含一次 ask 的所有关键指标 |
| 12:00-12:30 | 🔧 report 单测 | `tests/test_report.py` 1 test green |
| 14:00-15:30 | ⚡ 代码质量：① 全局 `ruff check` + `ruff format` ② 检查所有公开方法有 docstring ③ 检查所有异常路径有日志/Trace 记录 ④ 确认 `.agent/` 目录结构清晰 | CI 零 warning，代码可读 |
| 15:30-16:30 | ⚡ 端到端测试：用 FakeClient 预设一次完整的多步 ask 序列（read → search → write → patch → run_shell），验证：① 所有工具执行记录在 trace ② memory 正确更新 ③ quota 正确计数 ④ task_state 正确记录 step 和 stop_reason ⑤ report 完整 | 一次模拟覆盖全部模块 |
| 16:30-17:30 | 🔧 端到端测试用例编写 | `tests/test_e2e.py` 1 test green |

**Day 19 验收：** 所有模块协同工作不冲突。端到端测试通过。代码整洁。

---

### Day 20（周五）：真实 API 全流程 + M4 收尾 + M1-M4 总结

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:00 | ⚡ 真实 API 测试语义记忆：跑 3 轮对话，使用同义词查询，验证语义检索命中（如 "test setup" 命中 "pytest fixture"） | 语义检索生效 |
| 10:00-11:00 | ⚡ 真实 API 测试配额：连续请求 write_file 直至超限，验证配额拦截后 Agent 正确处理（改为 read-only 建议或终止） | 配额拦截 Agent 行为合理 |
| 11:00-12:00 | ⚡ 真实 API 测试 Circuit Breaker：临时改 `.env` 中 API key 为无效值 → 运行 Agent → 观察熔断触发 | 熔断后 Agent 优雅终止 |
| 12:00-12:30 | 🔧 修复真实 API 测试中发现的问题 | 最后一轮修 bug |
| 14:00-15:30 | ⚡ 全量测试通过：`pytest tests/ -v --cov=agent_runtime` 全部绿色，覆盖率 > 70% | 目标 70+ tests green |
| 15:30-16:30 | ⚡ M1-M4 代码统计 + 文件清单整理。git tag m4-done | M4 正式完成 |
| 16:30-17:30 | ⚡ Layer 1 完成复盘：记录总行数、测试数、覆盖的 bullet 点、遗留的技术债。为 M5 多 Agent 做准备 | Layer 1 完成 |

**Day 20 验收（M4 里程碑）：**
- [ ] 语义记忆生效，降级方案可用
- [ ] 4 种 Provider 全部可用
- [ ] 工具配额正确限制
- [ ] Circuit Breaker 正确熔断和恢复
- [ ] Deterministic Replay 可回放
- [ ] 进度回调清晰
- [ ] 工具降级链生效
- [ ] `pytest tests/ -v --cov` 全绿，70+ tests，覆盖率 > 70%
- [ ] 代码量约 1900 行（M1 500 + M2 500 + M3 500 + M4 400）

---

## 附录 A：M3-M4 新增文件清单

```
agent_runtime/
├── features/
│   └── memory.py           # M3: Day11-12, M4: Day16（+SemanticMemory）
├── task_state.py           # M3: Day12
├── checkpoint.py           # M3: Day13
├── session_store.py        # M3: Day12
├── run_store.py            # M3: Day12
├── security.py             # M2: Day6 + M3: Day13（扩展）
├── callbacks.py            # M4: Day17
├── replay.py               # M4: Day18
└── providers/
    ├── clients.py          # M4: Day16（+Ollama +OpenAI）
    └── circuit_breaker.py  # M4: Day18

tests/
├── test_memory.py          # M3: Day11
├── test_memory_hooks.py    # M3: Day11
├── test_durable_memory.py  # M3: Day12
├── test_persistence.py     # M3: Day12
├── test_checkpoint.py      # M3: Day13
├── test_resume.py          # M3: Day13
├── test_shrink.py       # M3: Day14
├── test_task_state.py      # M3: Day14
├── test_trace.py           # M3: Day14
├── test_semantic_memory.py # M4: Day16
├── test_providers.py       # M4: Day16
├── test_quota.py           # M4: Day17
├── test_callbacks.py       # M4: Day17
├── test_fallback.py        # M4: Day17
├── test_circuit_breaker.py # M4: Day18
├── test_replay.py          # M4: Day18
├── test_report.py          # M4: Day19
└── test_e2e.py             # M4: Day19
```

## 附录 B：M1-M4 累计指标

| 里程碑 | 代码量 | 测试数 | 核心能力 |
|:--:|:--:|:--:|------|
| M1 | 500 行 | 20 | 控制循环 + 3 工具 + Config + Workspace |
| M2 | 1000 行 | 40 | 6 工具 + 7 闸口 + Token 预算 + Dry-Run + REPL |
| M3 | 1500 行 | 55 | 三层记忆 + Checkpoint + 持久化 + 安全 + 对话摘要 |
| M4 | 1900 行 | 70 | 语义记忆 + 配额 + 熔断 + Replay + 4 Provider + 降级 |

**Layer 1 完成后的 Agent 运行时能力矩阵：**

| 能力 | 状态 |
|------|:--:|
| ReAct 控制循环 | ✅ |
| 6 个基础工具（3 读 + 3 写） | ✅ |
| 7 道工具安全闸口 | ✅ |
| Token 级上下文预算 | ✅ |
| 历史智能压缩 + 对话摘要 | ✅ |
| 三层工作记忆 + 语义检索 | ✅ |
| Durable Memory 持久化 | ✅ |
| Checkpoint / Resume | ✅ |
| 运行审计（task_state + trace + report） | ✅ |
| 4 种 Provider（Anthropic / Ollama / OpenAI / Fake） | ✅ |
| Dry-Run 预览 | ✅ |
| 工具配额 | ✅ |
| Circuit Breaker 熔断 | ✅ |
| Deterministic Replay | ✅ |
| 进度回调 + 工具降级 | ✅ |
| REPL 交互 + CLI one-shot | ✅ |
