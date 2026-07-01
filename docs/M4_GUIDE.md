# M4 GUIDE — 语义记忆 + 配额熔断 + 高级能力

> M4 在 M1-M3 的可靠 Agent 内核之上，加入了语义检索、执行配额、API 熔断、行为回放和进度回调——这些是 LangChain 等框架完全没有的能力，也是面试中拉开差距的关键。

---

## 1. M4 vs M3：新增了什么

| 能力 | M3 状态 | M4 新增 |
|------|------|------|
| Provider | 2（Anthropic / Fake） | **4**（+ Ollama / OpenAI） |
| 记忆检索 | keywords 精确匹配 | **semantic embedding 语义检索**（同义词/变体） |
| 工具执行 | 9 道闸口 | **+ 配额控制**（writes/shell/total） |
| API 韧性 | 3 次重试 | **Circuit Breaker 三态熔断**（失败不再等待） |
| 可观测性 | trace.jsonl 记录 | **ProgressCallback 实时进度** |
| 调试 | 无 | **Deterministic Replay**（从 trace 回放对比） |

---

## 2. 新增模块

### 2.1 语义记忆 — Semantic Memory

**`features/memory.py`** (+131 行)

```
SemanticMemory
├── 模型: all-MiniLM-L6-v2 (80MB, 本地运行)
├── encode(text)  → embedding vector
├── search(query) → cosine_similarity > 0.3 阈值
└── 降级: 模型不可用 → 静默退化为 keywords-only

retrieval_candidates_semantic(state, query)
  → keywords 路（快速精确） + semantic 路（同义词补充）
  → 合并去重 → top_k
```

### 2.2 API 熔断 — Circuit Breaker

**`providers/circuit_breaker.py`** (83 行)

```
三态状态机:
  CLOSED ──连续失败≥5──→ OPEN ──30s──→ HALF_OPEN
    ↑                                      │
    └─────成功─────────────────────────────┘
          失败──────────────────→ OPEN

call(fn) 包裹执行:
  - CLOSED: 正常调用，记录成功/失败
  - OPEN: 立即抛 CircuitBreakerOpenError（不等待超时）
  - HALF_OPEN: 允许 1 次探测调用
```

### 2.3 工具配额 — QuotaEnforcer

**`tool_executor.py`** (+70 行)

```
配额类型:
  - writes: 20/会话（write_file + patch_file）
  - shell:  10/会话（run_shell）
  - total:  50/会话（全部工具）

ToolExecutor Gate 4: check(name) → 超限拒绝
Gate 9 后: record(name) → 计数器 +1
```

### 2.4 Ollama + OpenAI 客户端

**`providers/clients.py`** (+107 行)

| 客户端 | 端点 | 特点 |
|------|------|------|
| `OllamaModelClient` | `/api/generate` | 本地模型，零网络费用 |
| `OpenAICompatibleModelClient` | `/v1/responses` | Responses API，usage 提取 |

### 2.5 进度回调与回放 — ProgressCallback + Replay

**`callbacks.py`** (44 行)
```
ProgressCallback Protocol:
  on_step_start(step, max_steps)
  on_tool_executed(name, result_preview)  → ✅/❌
  on_final_answer(text)
```

**`replay.py`** (75 行)
```
ReplayRunner(trace_path)
  → 读取 trace.jsonl
  → 回放 tool_executed 事件
  → 对比 实际结果 vs 记录结果
  → ReplayResult(matches, diffs, errors)
```

---

## 3. Layer 1 完成总览

```
agent_runtime/ (19 源文件, ~4400 行)

Layer 1 = M1 + M2 + M3 + M4 = 26 PRs, 6442 行, 215 tests
```

### 架构全景

```
CLI (--provider / --resume / --dry-run / --replay)
    │
    ▼
Agent ──CircuitBreaker──→ 模型 (Anthropic / Ollama / OpenAI / Fake)
    │
    ├── ToolExecutor (9-gate: allowed_tools→existence→validation→
    │   quota→duplicate→dry_run→approval→snapshot→execute)
    │
    ├── Memory (4-layer: Working→Episodic→Durable→Semantic)
    │
    ├── ContextManager (TokenBudget + HistoryCompression + Summarization)
    │
    └── Persistence (TaskState→RunStore→Checkpoint→SessionStore)
```

### 能力矩阵

| 能力 | 实现模块 | M |
|------|------|:--:|
| 控制循环 | agent_loop.py | M1 |
| 6 工具 + auto_schema | tools.py, schema_utils.py | M1/M2 |
| 模型客户端 (4 provider) | providers/clients.py | M1/M4 |
| System Prompt + Cache | prompt_prefix.py | M1/M2 |
| 7→9 道闸口 | tool_executor.py | M2/M4 |
| Token 预算 + 摘要 | context_manager.py | M2/M3 |
| 安全 (shell_env + redact) | security.py | M2/M3 |
| 4 层记忆 | features/memory.py | M3/M4 |
| 持久化 + Trace | task_state, run_store, session_store | M3 |
| 恢复 + Checkpoint | checkpoint.py, runtime.from_session | M3 |
| Circuit Breaker | providers/circuit_breaker.py | M4 |
| Quota + Callbacks + Replay | tool_executor, callbacks, replay | M4 |

### 里程碑

| Tag | 内容 | PRs | 行数 | Tests |
|------|------|:--:|:--:|:--:|
| `m1-done` | Agent 内核 | #1-#7 | 2284 | 73 |
| `m2-done` | 工具系统 + 预算 | #8-#15 | +1291 | +47 |
| `m3-done` | 记忆 + 持久化 | #16-#21 | +1984 | +73 |
| `m4-done` | 高级能力 | #22-#26 | +887 | +22 |
| **Layer 1** | | **26 PRs** | **6442** | **215** |

---

## 4. M4 后改进（PR #27～#34）

> M4 sprint 结束后持续补全的模块接线、测试覆盖和工程质量提升。

### 接线修复（5 个"已完成但未接入"的模块）

| PR | 模块 | 问题 | 修复 |
|:--:|------|------|------|
| #34 | CircuitBreaker | Agent 初始化了但 AgentLoop 未用 | `cb.call()` 包裹模型调用，熔断时优雅终止 |
| #34 | QuotaEnforcer | Agent 创建了实例但 ToolExecutor 收不到 | `execute_tool()` 传 `self.quota` |
| #34 | redact_artifact | 实现了但 trace/report 写入前未调 | `append_trace` + `write_report` 写入前自动脱敏 |
| #34 | promote_durable_memory | 实现了但无人调用 | `AgentLoop._finalize_run` 自动保存 |
| #34 | _maybe_summarize_history | 实现了但 history 压缩不用它 | `_get_compressed_history` 优先 LLM 摘要，降级规则压缩 |
| #33 | M3 Memory | Working/Episodic Memory 已实现但 prompt 中为空 | `_get_memory()` + `_get_relevant()` 返回记忆内容 |
| — | Quota CLI | 配额硬编码无法配置 | `--quota-writes/--quota-shell/--quota-total` CLI 参数 |
| — | ProgressCallback | 实现了但 CLI 未传入 | one-shot + REPL 均传 `CLIProgressCallback` |
| — | Agent.ask() | 不支持 callback 参数 | `ask(user_message, callback=None)` |
| — | Session 自动保存 | 会话不持久化 | `_finalize_run` 调 `SessionStore.save()` |
| — | Durable retrieval | 持久记忆只写不读 | `_get_relevant` 查询 `DurableMemoryStore` |
| — | `_maybe_summarize_history` | 接入 `_get_compressed_history` | 优先 LLM 摘要，降级规则压缩 |

### 功能增强

| PR | 改动 | 说明 |
|:--:|------|------|
| #29 | light-client + token quota | light_client 支持、max_new_tokens 2048、摘要配额适配 thinking 模型 |
| #30 | timestamp run_id | 运行目录从 UUID 改为 `YYYYMMDD-HHMMSS` 格式 |

### 测试与规范

| PR | 改动 | 说明 |
|:--:|------|------|
| #31 | 覆盖率提升 | +21 tests：CLI / schema_utils 边界 / providers / replay，覆盖率 80%→84% |
| #32 | gitignore | 排除 `.coverage` 临时文件 |
| — | 接线模块测试 | +8 tests：Quota / Callback / SessionSave / DurableRetrieval / CB |
| — | `bonus_memory.md` | 29 项记忆系统改进（Working/Episodic/Durable/Semantic） |
| — | `bonus_context.md` | 30 项上下文工程改进（TokenBudget/ContextManager/Summarization/Cache） |
| #27 | M4_GUIDE | 本文档 |

### 安全修复

| PR | 改动 | 说明 |
|:--:|------|------|
| #34 | `looks_sensitive_env_name` | 修复子串误判：`total_tokens` 不再因含 `TOKEN` 被脱敏 |

### 更新后的 Layer 1 数据

| 指标 | M4 sprint 结束 | 当前 |
|------|:--:|:--:|
| PR 总数 | 26 | **36** |
| 测试数 | 215 | **254** |
| 覆盖率 | — | **84%（行）/ 86%（分支）** |
| 总行数 | 6442 | **~7200** |
| 源码行数 | — | **~4000** |
| 测试行数 | — | **~3200** |
| Bonus 文档 | 2 | **4**（m1-m2 / m3-m4 / memory / context） |

---

*M4 完成日期：2026-07-01 | 最终更新：2026-07-02 | git tag: m4-done | 254 tests green | ~7200 total LOC | Layer 1 竣工*
