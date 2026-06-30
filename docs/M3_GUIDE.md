# M3 GUIDE — 记忆系统 + 持久化 + 会话恢复 + 对话摘要

> M3 让 Agent 从"每次调用都是全新的"变成**"有记忆、可恢复、有审计"**。

---

## 1. M3 vs M2：新增了什么

| 能力 | M2 状态 | M3 新增 |
|------|------|------|
| 记忆 | 无（session.history 是原始对话文本） | **三层记忆**：Working / Episodic / Durable |
| 持久化 | 无 | **Session JSON + Run 工件（JSONL trace + 原子写）** |
| 会话恢复 | 不支持 | **`--resume latest`**（含文件变更 + identity 检测） |
| 审计 | 无 | **每次 ask() 产出 task_state.json + trace.jsonl + report.json** |
| 安全 | shell_env 白名单 + redact_text | **redact_artifact（递归 dict/list/str 脱敏）** |
| 历史管理 | 规则压缩（合并/截断） | **LLM 驱动的对话摘要**（含降级方案） |

---

## 2. 架构全景

```
Agent.ask("用户输入")
    │
    ├── [新建] AgentLoop 创建 TaskState → emit_trace("run_started")
    │
    while True:
        ├── Agent.prompt() → ContextManager
        │     └── 历史超 2600t → _maybe_summarize_history()
        │           ├── 成功 → [LLM摘要] + 近期历史
        │           └── 失败 → 裁剪保留最近 8 条
        │
        ├── Client.complete() → emit_trace("model_requested")
        ├── Agent.parse()
        │
        ├── tool → Agent.execute_tool() → ToolExecutor
        │     └── Agent.update_memory_after_tool()
        │           ├── read_file → remember_file + set_file_summary
        │           ├── write/patch → remember_file + invalidate
        │           ├── run_shell → append_note
        │           └── search → append_note
        │     └── emit_trace("tool_executed")
        │
        └── final → finish_success() → emit_trace("run_finished")
              └── _finalize_run()
                    ├── write_task_state()    (.agent/runs/{id}/task_state.json)
                    ├── write_report()        (.agent/runs/{id}/report.json)
                    └── create_checkpoint()   (session.checkpoints[])
```

---

## 3. 新增模块地图（6 个文件）

### 3.1 三层记忆系统

**`features/memory.py`** (553 行)

```
Working Memory (容量小，频繁读写)
├── task_summary      300 字摘要（set_task_summary）
├── recent_files      LRU 8 个（remember_file：去重 + trim）
└── file_summaries    6 个，带 freshness hash
    ├── set_file_summary(path, summary)
    └── invalidate_file_summary(path)  — write/patch 后自动失效

Episodic Memory (会话级事件笔记，FIFO 12 条)
├── append_note(text, tags, source, kind)
│   去重：最后一条内容相同 → 不追加
└── retrieval_candidates(query, limit=3)
    排序：tag 精确 = 3.0 > keyword = 1.0 > recency <= 1.0

Durable Memory (跨会话持久化，Markdown 文件)
├── DurableMemoryStore(root)
│   ├── promote([(topic, text)])   → topics/{topic}.md
│   ├── retrieval(query)           → 全文搜索
│   └── _upsert_entry()            → 首行相同自动替换
└── promote_durable_memory(user_msg, answer)
    ├── _has_save_intent()          → detect "remember"/"记住"
    ├── _extract_promotions()       → Convention:/Decision:/Dependency:/Preference:
    └── reject_durable_reason()     → 空/短/长/含密钥 → 拒绝
```

**记忆目录结构**：
```
.agent/memory/
├── MEMORY.md                    # 索引
└── topics/
    ├── project-conventions.md
    ├── key-decisions.md
    ├── dependency-facts.md
    └── user-preferences.md
```

### 3.2 运行状态机

**`task_state.py`** (109 行)

```python
状态转换：
  running ── finish_success() ──→ completed (stop_reason="final")
         ├─ stop_step_limit() ──→ stopped
         └─ stop_retry_limit() ─→ failed

记录字段：run_id, task_id, user_request, tool_steps, attempts,
          last_tool, stop_reason, final_answer, checkpoint_id, resume_status

序列化：to_dict() / from_dict() 往返
```

### 3.3 持久化存储

**`session_store.py`** (73 行)

```python
store = SessionStore(root)
store.save(session)        # → .agent/sessions/{id}.json（原子写）
store.load(id)             # → dict | None
store.latest()             # → 最近修改的 session id（按 mtime）
```

**`run_store.py`** (94 行)

```python
store = RunStore(root)
store.start_run(ts)        # → .agent/runs/{run_id}/
store.write_task_state(ts) # → task_state.json（原子写 .tmp → replace）
store.append_trace(ts, e)  # → trace.jsonl（JSONL 逐行追加）
store.write_report(ts, r)  # → report.json（原子写）
```

### 3.4 跨轮恢复

**`checkpoint.py`** (134 行)

```python
# 创建检查点（每次 ask 结束自动调用）
create_checkpoint(agent, task_state, user_message)
  → 记录 current_goal + key_files (freshness hash) + runtime_identity

# 评估恢复状态
evaluate_resume_state(agent) → {
    "status": "no-checkpoint" | "full-valid" | "partial-stale" |
              "workspace-mismatch" | "schema-mismatch",
    "stale_files": [...],      # freshness 不匹配的文件
    "identity_diff": [...],    # 变化的 config 字段
}

# Runtime 身份组成：cwd, provider, model, approval, max_steps, tools_signature
```

### 3.5 安全增强

**`security.py`** (+34 行)

```python
redact_artifact(value, secret_values)
  ├── dict  → 递归。key 名敏感 → "<redacted>"
  ├── list  → 递归每个元素
  ├── str   → 匹配 secret_values → 替换为 "<redacted>"
  └── other → 原样返回
```

### 3.6 对话摘要

**`context_manager.py`** (+68 行)

```python
ContextManager._maybe_summarize_history(history, trigger_tokens=2600)
  ├── token 数 <= 2600 → 原样返回
  ├── 超限 → 取前一半 → model_client.complete("Summarize...")
  │     ├── 成功 → [{"role":"system","content":"[Earlier summary]: ..."}, *recent]
  │     └── 失败 → 降级保留最近 8 条
```

---

## 4. 关键设计决策

### 4.1 为什么记忆分三层而不是一个大 JSON？

不同记忆有不同的访问频率和容量需求：
- **Working Memory**：每轮都读（容量小、访问快）。Agent 需要快速回答"我刚读了哪些文件？"
- **Episodic Memory**：按需检索（容量有限、FIFO）。工具执行异常时需要回溯"刚才发生了什么"
- **Durable Memory**：跨会话保留（容量大、访问慢）。用户说"记住我偏好 pytest"应该永久保存

### 4.2 为什么 Durable Memory 用 Markdown 而不是 SQLite？

- Markdown 文件**人类可读**，用户可以手动编辑 `topics/user-preferences.md`
- **Git diff 可见**：记忆变更可以纳入版本控制
- 对于"项目知识库"这种写入频率低、阅读频率高的场景，文件系统比数据库更合适

### 4.3 为什么 run_store 用原子写（.tmp → replace）？

中途 kill 进程（Ctrl+C / OOM / 崩溃）时，如果用 direct write，可能得到一个半截 JSON 文件。原子写保证"要么完整写入，要么什么都没有"。

### 4.4 为什么 trace 用 JSONL 追加而不是最后一次性写？

- **实时可读**：Agent 运行中可以用 `tail -f trace.jsonl` 查看进度
- **崩溃安全**：已追加的事件不会因为最终写入失败而丢失
- **增量处理**：可以逐行 parse，不需要把整个 trace 加载到内存

### 4.5 为什么 resume 检查文件 freshness 和 runtime identity？

Agent 离线期间文件可能被外部修改（git checkout、编辑器等）。如果盲目恢复，Agent 可能基于过时的文件摘要做判断。

Runtime identity 检查确保恢复时的配置（model/provider/approval）与创建时一致——换了模型或关了审批可能改变 Agent 行为。

### 4.6 为什么对话摘要失败时退化为保留最近 8 条而不是扔异常？

摘要是"优化体验"而非"必需功能"。API 失败不应该让用户请求崩溃。保留最近 8 条是一个安全且确定的降级方案——丢失了早期上下文，但不会比没有摘要更差。

---

## 5. 数据流：一次 ask() 的完整产物

```python
agent.ask("fix the TypeError")

# 运行中：
session.memory.working.recent_files = ["calc.py", "utils.py"]
session.memory.file_summaries = {"calc.py": {...}, "utils.py": {...}}
session.memory.episodic_notes = [
    {"text": "read_file returned 33 lines", "kind": "observation"},
    {"text": "搜索 'TypeError': found at line 42", "kind": "observation"},
]
session.checkpoints = [
    {"current_goal": "fix the TypeError", "key_files": {"calc.py": "abc123"}},
]

# 运行后（.agent/runs/{run_id}/）：
task_state.json  → {"status":"completed","tool_steps":2,"stop_reason":"final"}
trace.jsonl      → {"event":"run_started","created_at":"..."}
                    {"event":"tool_executed","payload":{"tool":"read_file"}}
                    {"event":"tool_executed","payload":{"tool":"patch_file"}}
                    {"event":"run_finished","payload":{"stop_reason":"final"}}
report.json      → {"run_id":"a1b2c3d4","tool_steps":2,"status":"completed"}
```

---

## 6. 测试策略

```
tests/
├── test_memory.py             (19 tests)  # Working + Episodic
├── test_memory_hooks.py       (7 tests)   # Agent 记忆钩子集成
├── test_durable_memory.py     (17 tests)  # DurableMemoryStore + promote
├── test_persistence.py        (12 tests)  # TaskState + Session/Run Store
├── test_checkpoint_resume.py  (12 tests)  # Checkpoint + redact + from_session
├── test_summarization_taskstate.py (6 tests)  # 摘要 + TaskState 集成
└── (M1+M2 tests)              (120 tests) # 回归

M3 总计: +73 tests, 193 total
```

---

## 7. 快速上手

```bash
# 恢复上次会话
python -m agent_runtime --resume latest

# 恢复指定 session
python -m agent_runtime --resume <session_id>

# 运行时检查恢复状态
/session        # → 显示 run_id / 轮数 / checkpoint status

# 查看记忆
/memory         # → Working Memory 快照

# 持久化目录
ls .agent/sessions/    # 会话 JSON
ls .agent/runs/        # 运行工件
ls .agent/memory/      # Durable Memory
```

---

*M3 完成日期：2026-07-01 | git tag: m3-done | 193 tests green | 5559 total LOC*
