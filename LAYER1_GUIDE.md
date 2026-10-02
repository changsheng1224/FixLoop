# Layer 1 导读 — Agent 运行时内核全貌

> 读完本文你将理解：254 tests / 33 source files / 4000 行代码的完整 Agent 运行时是怎么组织的，每个模块干什么、怎么连接。

---

## 1. 一分钟概览

```
用户敲下命令
    │
    ▼
CLI (cli.py) — 装配 Config + Workspace + ModelClient → Agent
    │
    ▼
Agent (runtime.py) — 对外唯一接口
    │
    ├── ask() → AgentLoop (agent_loop.py) — 控制循环
    │     ├── prompt → ContextManager (context_manager.py) — Token 预算 + 历史压缩
    │     ├── complete → ModelClient (providers/clients.py) — HTTP 调模型
    │     ├── parse → Agent.parse() — 提取 tool/final/retry
    │     ├── execute_tool → ToolExecutor (tool_executor.py) — 9 道闸口
    │     │     └── Tool (tools.py) — 6 个工具的实际执行
    │     └── record → session.history + update_memory
    │
    ├── Memory (features/memory/) — 4 层记忆
    ├── Security (security.py) — 3 层防护
    ├── Persistence (task_state + session_store + run_store)
    ├── Checkpoint (checkpoint.py) — 跨轮恢复
    ├── CircuitBreaker (providers/circuit_breaker.py) — API 熔断
    └── Replay (replay.py) — 行为回放
```

---

## 2. 文件地图（按功能分组）

### 2.1 入口与装配

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `cli.py` | 310 | M1/B | argparse + 装配管线 + REPL + --profile/--health/--dry-run |
| `__main__.py` | 7 | M1 | `python -m agent_runtime` 入口 |
| `__init__.py` | 4 | M1 | 公开 API 导出 |

### 2.2 配置与工作区

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `config.py` | 27 | M1 | AgentConfig(pydantic) — provider/model/max_steps/approval/temperature |
| `workspace.py` | 87 | M1 | WorkspaceContext — git info + 白名单文档 + SHA256 指纹 |

### 2.3 Agent 核心

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `runtime.py` | 310 | M1/M3/M4 | Agent 类：构造装配、ask()、parse()、记忆钩子、from_session |
| `agent_loop.py` | ~1960 | M1/M3/M4/B | AgentLoop：单一主循环、生命周期、预算、停止决策和回调装配 |
| `loop_protocols.py` | ~800 | M8 | Native/XML 单轮模型调用、上下文、输出恢复与答案校验 |
| `tool_step_runtime.py` | ~860 | M8 | 工具预检、执行暂停边界、观察记录、补丁恢复和进展更新 |

### 2.4 模型后端

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `providers/clients.py` | 270 | M1/M4/B | Fake/Anthropic/Ollama/OpenAI + 请求重放 + latency_stats |
| `providers/circuit_breaker.py` | 83 | M4 | 三态熔断（CLOSED/OPEN/HALF_OPEN） |

### 2.5 工具系统

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `tools.py` | 410 | M1/M2/B | 6 工具 + write_file append + grep context_lines |
| `schema_utils.py` | 51 | M1 | auto_schema() + auto_validate() — 从 type hints 推导 |
| `tool_context.py` | 24 | M1 | ToolContext — 路径解析 + 逃逸检测 |
| `tool_executor.py` | 310 | M2/M4 | 9 闸口 ToolExecutor + QuotaEnforcer + 快照对比 |

### 2.6 上下文管理

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `prompt_prefix.py` | 53 | M1/M2/B | System Prompt + dry-run/approval 动态规则注入 |
| `context_manager.py` | 360 | M2/M3/M4/B | TokenBudget + 5-section 组装 + 智能截断 + LLM 摘要 + 缓存 |

### 2.7 记忆系统

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `features/memory/core.py` | 55 | M3 | 初始化 + 规范化 + 常量 |
| `features/memory/working.py` | 42 | M3 | Working Memory — task_summary/recent_files/file_summaries |
| `features/memory/episodic.py` | 50 | M3/B | Episodic Memory — append_note/retrieval_candidates（含匹配分数） |
| `features/memory/durable.py` | 115 | M3/B | Durable Memory — Markdown 存储/检索（含 topic 标注） |
| `features/memory/semantic.py` | 75 | M4/B | Semantic Memory — embedding/cosine + HF 镜像支持 |

### 2.8 持久化与恢复

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `task_state.py` | 60 | M3/B | TaskState 状态机 + node_timings 耗时分布 |
| `session_store.py` | 65 | M3 | JSON 原子写 + latest() |
| `run_store.py` | 95 | M3 | task_state.json + trace.jsonl + report.json（含 token_usage） |
| `checkpoint.py` | 120 | M3 | create_checkpoint + evaluate_resume_state(5 状态) |

### 2.9 安全与辅助

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `security.py` | 135 | M2/M3 | shell_env + redact_text + redact_artifact + looks_sensitive |
| `callbacks.py` | 50 | M4/B | AgentCallback + CLIProgressCallback（ANSI 彩色 + 耗时） |
| `replay.py` | 75 | M4 | ReplayRunner — 从 trace 回放工具执行 |

---

## 3. 数据流追踪

### 3.1 一次 ask() 的完整路径

普通运行和 checkpoint 恢复统一进入 `_run_loop()`：检查停止条件 → 获取规范化回复 → 执行工具、恢复或接受答案 → 记录并继续／结束。
`loop_protocols.native_model_turn()` 和 `xml_model_turn()` 各处理一次模型交互，使用现有 `CanonicalResponse` 表达工具调用、最终答案和异常；适配函数自身不控制循环。
Native 按模型轮次计数，XML 保留工具步数和解析尝试计数。恢复入口按 checkpoint 的协议选择适配函数，避免因客户端支持 Native 而切换已有 XML 会话。
Native 的截断和空输出共享一次恢复机会，不将不完整内容写入历史；XML 的 JSON 校验沿用有上限的解析恢复。答案和终态工具统一通过 `_finish_answer()` → `_complete_run()` 收尾。
上下文、工具批次和落盘继续使用已有模块；预算、取消、验证和终态契约不增加新的状态机或调度层。

`tool_step_runtime` 负责工具预检 → 暂停并交给执行器 → 提交观察 → 恢复/进展记录。
Native 批次与 XML 单工具共用该流程，保留 generator 的 yield/send/throw 边界和 owner 提交顺序。
协议和工具模块接收明确的状态、资源和回调，不持有整个 `AgentLoop`；状态在一次运行中共享，checkpoint/report 的字段不变。

```text
Agent.ask(user_message, callback)
  → AgentLoop.run()
      ├── 普通运行：创建 TaskState、初始化上下文、可选 Plan
      ├── step resume：恢复 TaskState / 预算 / deadline，保留 checkpoint 协议
      └── _run_loop()
            ├── 检查取消、期限、轮次 / 工具步数、预算
            ├── 获取 CanonicalResponse
            │     ├── loop_protocols.native_model_turn()：Native 消息与 complete_turn()
            │     └── loop_protocols.xml_model_turn()：文本调用与 parse_model_response()
            ├── 工具调用 → 执行、记录观察 → 下一轮
            │     ├── Native：ToolBatchRunner，按调用 ID 配对结果
            │     └── XML：单个 _run_tool_step，更新后续消息
            │           （两者共用 tool_step_runtime.tool_step_flow）
            ├── 回复异常 → 有限恢复 → 下一轮或停止
            └── 最终答案 / 终态工具 → _finish_answer()
                                      → _complete_run()
                                          ├── 关闭 turn progress、提交唯一终态
                                          ├── 回调、run_terminal / run_finished
                                          └── _finalize_run() → finalize_agent_run()
                                                ├── checkpoint / task_state / report
                                                ├── 记忆反馈与维护
                                                └── SessionStore.save()
```

### 3.2 各模块编写顺序

```
M1 (地基):
  config → workspace → clients(Fake+Anthropic) → tools(3只读)
  → prompt_prefix → runtime(parse) → agent_loop → cli

M2 (工具体系):
  tools(+3写) → tool_context → security(shell_env) → tool_executor(7闸口)
  → context_manager(Token+压缩) → clients(cache) → cli(DryRun+REPL)

M3 (记忆+持久化):
  features/memory(Working+Episodic) → memory(Durable)
  → task_state → session_store → run_store
  → checkpoint → security(redact) → context(摘要)

M4 (高级能力):
  memory(Semantic) → clients(Ollama+OpenAI)
  → circuit_breaker → callbacks → replay → tool_executor(Quota)

Bonus (工具/体验/性能增强):
  tools(append+context_lines) → clients(replay+latency) → callbacks(color+timing)
  → CB(status) → task_state(timings) → context(truncate+cache)
  → prompt(dynamic rules) → agent_loop(backoff) → memory(scores+topic+mirror)
  → cli(profile+health) → tests(+35)
```

---

## 4. 关键设计模式

### 4.1 工厂函数 > 子类化

4 个 Provider 都是独立类（不是子类），通过相同的 `complete()` 签名实现多态。未来新增 Provider 只需实现 `complete(prompt, max_new_tokens, prompt_cache_key) -> str`。

### 4.2 延迟导入打破循环

`runtime.py` ↔ `agent_loop.py` ↔ `context_manager.py` ↔ `tool_executor.py` 之间存在循环依赖。使用函数内 `import` 延迟加载解决——import 只在实际调用时发生，此时所有模块已加载完毕。

### 4.3 不抛异常

`ToolExecutor.execute_gated()` 的 9 道闸口任何一道失败都返回 `ToolResult`，不抛异常。AgentLoop 拿到的始终是结构化结果，不会因闸口拒绝而崩溃。模型可以读错误信息并调整策略。

### 4.4 单例在构造时完成

Agent 的 `__init__` 完成所有装配：tool registry、prompt prefix、circuit breaker、quota、semantic memory、session/memory 初始化。一次构造 = 一次完整的 Agent 就绪。

---

## 5. 测试地图

| 文件 | 测试 | 覆盖模块 |
|------|:--:|------|
| `test_config.py` | 8 | Config pydantic |
| `test_workspace.py` | 11 | WorkspaceContext |
| `test_clients_and_parse.py` | 12 | FakeClient + Agent.parse() |
| `test_anthropic_client.py` | 5 | Anthropic HTTP + _extract_text |
| `test_tools.py` | 19 | 6 工具 + registry + 逃逸 |
| `test_prompt_prefix.py` | 4 | System Prompt 构建 |
| `test_agent_loop.py` | 8 | 控制循环 + 停机 |
| `test_integration.py` | 6 | 完整 ask 管线 |
| `test_write_patch.py` | 8 | write_file + patch_file |
| `test_shell_security.py` | 6 | run_shell + redact |
| `test_tool_executor.py` | 12 | 9 闸口 + 快照 |
| `test_context_manager.py` | 13 | Token 预算 + 裁剪 + 压缩 |
| `test_cli.py` | 7 | _load_dotenv + _build_client |
| `test_cache_and_dryrun.py` | 7 | Prompt cache + dry-run |
| `test_memory.py` | 19 | Working + Episodic |
| `test_memory_hooks.py` | 7 | Agent 记忆钩子 |
| `test_durable_memory.py` | 17 | DurableMemoryStore |
| `test_persistence.py` | 12 | TaskState + Session/Run Store |
| `test_checkpoint_resume.py` | 12 | Checkpoint + redact + resume |
| `test_summarization_taskstate.py` | 6 | LLM 摘要 + TaskState 集成 |
| `test_semantic_memory.py` | 6 | Semantic 检索 + 降级 |
| `test_quota.py` | 7 | QuotaEnforcer |
| `test_callbacks.py` | 4 | CLIProgressCallback |
| `test_circuit_breaker.py` | 9 | CB 状态机 + Replay |
| `test_e2e.py` | 2 | M1-M4 全管线 |
| `test_light_client.py` | 10 | Ollama mock + 双模型 |
| `test_wired_modules.py` | 8 | 接线模块集成 |
| `test_schema_utils_edge.py` | 8 | auto_validate 边界 |
| `test_providers_replay.py` | 6 | OpenAI mock + ReplayRunner |
| `test_cli.py` | 7 | CLI 装配函数 |

**29 个测试文件，254 个测试。**

---

## 6. 运行时产物

每次 `agent.ask()` 后在 `.agent/` 下生成：

```
.agent/
├── runs/{YYYYMMDD-HHMMSS}/
│   ├── task_state.json    # 状态机快照
│   ├── trace.jsonl        # 逐事件时间线
│   └── report.json        # 运行摘要
├── sessions/{id}.json     # 会话持久化
├── last_request.json       # 最近 API 请求（调试用）
├── audit/                  # 闸口审计日志
└── memory/                # Durable Memory
    ├── MEMORY.md
    └── topics/
        ├── project-conventions.md
        ├── key-decisions.md
        ├── dependency-facts.md
        └── user-preferences.md
```

---

## 7. 快速启动

```bash
conda activate fixloop

# One-shot
python -m agent_runtime "what does config.py do?"

# REPL 多轮
python -m agent_runtime

# Dry-Run 预览
python -m agent_runtime --dry-run "fix the TypeError"

# 恢复上次会话
python -m agent_runtime --resume latest

# 本地模型加速摘要
python -m agent_runtime --light-provider ollama --light-model qwen3.5:9b

# CI 模式（零配额、零审批）
python -m agent_runtime --profile ci "fix the bug"

# 健康检查
python -m agent_runtime --health

# 全部测试
pytest tests/ -v

# 覆盖率
pytest tests/ --cov=agent_runtime --cov-branch
```

---

*Layer 1 完成 | 281 tests | 82% 行覆盖 / 86% 分支覆盖 | ~4300 行源码 | 13 bonus PRs*

当前配置、工具导入和 checkpoint 恢复边界见 [运行时契约](docs/RUNTIME_CONTRACTS.md)。
