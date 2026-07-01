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
| `cli.py` | 230 | M1/M4 | argparse → _make_config → _make_agent → ask() |
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
| `runtime.py` | 300 | M1/M3/M4 | Agent 类：构造装配、ask()、parse()、记忆钩子、from_session |
| `agent_loop.py` | 170 | M1/M3/M4 | AgentLoop：while 循环、停机条件、trace 发射、run 收尾 |

### 2.4 模型后端

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `providers/clients.py` | 270 | M1/M4 | FakeClient + AnthropicCompatible + Ollama + OpenAICompatible |
| `providers/circuit_breaker.py` | 83 | M4 | CLOSED/OPEN/HALF_OPEN 三态熔断 |

### 2.5 工具系统

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `tools.py` | 400 | M1/M2 | 6 工具 dataclass + 执行函数 + registry + fallback |
| `schema_utils.py` | 51 | M1 | auto_schema() + auto_validate() — 从 type hints 推导 |
| `tool_context.py` | 24 | M1 | ToolContext — 路径解析 + 逃逸检测 |
| `tool_executor.py` | 310 | M2/M4 | ToolExecutor(9闸口) + QuotaEnforcer + 快照对比 |

### 2.6 上下文管理

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `prompt_prefix.py` | 48 | M1/M2 | System Prompt — Persona + Rules + Tools + Examples + Workspace |
| `context_manager.py` | 290 | M2/M3/M4 | TokenBudget + 5-section 组装 + 历史压缩 + LLM 摘要 + Memory 检索 |

### 2.7 记忆系统

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `features/memory/__init__.py` | 15 | M3 | 重导出 |
| `features/memory/core.py` | 55 | M3 | 初始化 + 规范化 + 常量 |
| `features/memory/working.py` | 42 | M3 | Working Memory — task_summary / recent_files / file_summaries |
| `features/memory/episodic.py` | 50 | M3 | Episodic Memory — append_note / retrieval_candidates |
| `features/memory/durable.py` | 115 | M3 | Durable Memory — Markdown 存储 / promote / reject |
| `features/memory/semantic.py` | 75 | M4 | Semantic Memory — embedding / cosine similarity |

### 2.8 持久化与恢复

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `task_state.py` | 55 | M3 | TaskState 状态机 — running→completed/stopped/failed |
| `session_store.py` | 65 | M3 | JSON 原子写 + latest() |
| `run_store.py` | 95 | M3 | task_state.json + trace.jsonl + report.json |
| `checkpoint.py` | 120 | M3 | create_checkpoint + evaluate_resume_state(5 状态) |

### 2.9 安全与辅助

| 文件 | 行数 | M | 职责 |
|------|:--:|:--:|------|
| `security.py` | 135 | M2/M3 | shell_env + redact_text + redact_artifact + looks_sensitive |
| `callbacks.py` | 44 | M4 | ProgressCallback Protocol + CLIProgressCallback |
| `replay.py` | 75 | M4 | ReplayRunner — 从 trace 回放工具执行 |

---

## 3. 数据流追踪

### 3.1 一次 ask() 的完整路径

```
cli.py main()
  → _make_agent(args)
      ├── _load_dotenv()
      ├── _make_config(args) → AgentConfig (pydantic 校验)
      ├── WorkspaceContext.build(cwd) → git info + docs
      ├── _build_model_client(args) → AnthropicCompatibleClient (urllib)
      └── Agent(config, client, workspace,
                light_client, dry_run, quota)
            │
            ├── ToolContext(root) + build_tool_registry → 6 tools
            ├── default_memory_state() → session.memory
            ├── build_prompt_prefix() → _prefix (缓存)
            ├── CircuitBreaker() + QuotaEnforcer() + SemanticMemory()
            │
            └── agent.ask("问题", callback=CLIProgressCallback())
                  │
                  ▼
AgentLoop.run(user_message, callback)
  │
  ├── TaskState.create() → run_id="20260702-143025"
  ├── emit("run_started")
  ├── record({"role":"user", ...})
  ├── _gen_task_summary(user_message) → light_client 生成摘要
  │
  └── while True:
        ├── [停机检查: tool_steps>max? attempts超?]
        │
        ├── prompt = agent.prompt(user_message)
        │     └── ContextManager.build()
        │           ├── _get_prefix()        → System Prompt
        │           ├── _get_memory()        → task + files + summaries
        │           ├── _get_relevant()      → episodic + durable retrieval
        │           └── _get_compressed_history()
        │                 ├── _maybe_summarize_history() → LLM 摘要
        │                 └── _compress_old_entries()    → 规则压缩
        │
        ├── raw = circuit_breaker.call(client.complete)(prompt, cache_key)
        │     → POST https://api.deepseek.com/anthropic/v1/messages
        │
        ├── kind, payload = Agent.parse(raw)
        │     → "tool" / "final" / "retry"
        │
        ├── [final] → record → finish_success → emit → _finalize_run → return
        │
        ├── [tool] → execute_tool(name, args)
        │     └── ToolExecutor.execute()
        │           ├─ ① allowed_tools ② exists ③ validate ④ quota
        │           ├─ ⑤ duplicate ⑥ dry_run ⑦ approval ⑧ snapshot
        │           └─ ⑨ execute → snapshot diff
        │     → update_memory_after_tool → record → emit → callback 显示
        │
        └── [retry] → record 纠错 → user_message=纠错提示

_finalize_run(ts):
  ├── create_checkpoint → session.checkpoints
  ├── write_task_state   → .agent/runs/{id}/task_state.json
  ├── write_report       → .agent/runs/{id}/report.json
  ├── promote_durable_memory → .agent/memory/topics/
  └── SessionStore.save  → .agent/sessions/{id}.json
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
```

---

## 4. 关键设计模式

### 4.1 工厂函数 > 子类化

4 个 Provider 都是独立类（不是子类），通过相同的 `complete()` 签名实现多态。未来新增 Provider 只需实现 `complete(prompt, max_new_tokens, prompt_cache_key) -> str`。

### 4.2 延迟导入打破循环

`runtime.py` ↔ `agent_loop.py` ↔ `context_manager.py` ↔ `tool_executor.py` 之间存在循环依赖。使用函数内 `import` 延迟加载解决——import 只在实际调用时发生，此时所有模块已加载完毕。

### 4.3 不抛异常

`ToolExecutor.execute()` 的 9 道闸口任何一道失败都返回 `ToolExecutionResult`，不抛异常。AgentLoop 拿到的始终是结构化结果，不会因闸口拒绝而崩溃。模型可以读错误信息并调整策略。

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

# 全部测试
pytest tests/ -v

# 覆盖率
pytest tests/ --cov=agent_runtime --cov-branch
```

---

*Layer 1 完成 | 254 tests | 84% 行覆盖 / 86% 分支覆盖 | ~4000 行源码*
