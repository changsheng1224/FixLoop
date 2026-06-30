# M2 GUIDE — 完整工具系统 + Token 预算 + 安全闸口

> 在 M1 的 Agent 内核之上，M2 构建了完整的工具执行安全体系、Token 级精确预算控制、Dry-Run 演习模式和 REPL 交互界面。

---

## 1. M2 vs M1：新增了什么

| 能力 | M1 状态 | M2 新增 |
|------|------|------|
| 工具数量 | 3（list/read/search） | **6**（+write/patch/shell） |
| 工具执行 | 直接调用执行函数 | **7 道安全闸口**（ToolExecutor） |
| Token 计数 | 字符数估算（中文误差 3-5 倍） | **tiktoken 精确计数**（误差 <5%） |
| Prompt 组装 | Agent.prompt() 简单拼接 | **ContextManager 5-section 预算体系** |
| 历史管理 | 逐条记录 | **智能压缩**（最近 6 条完整 + 旧条目合并/摘要） |
| 敏感信息 | 无 | **shell_env 白名单 + redact_text 脱敏** |
| 模型调用 | 每次全新发 prompt | **Prompt Cache**（prefix SHA256 → cache_control） |
| 文件修改 | 无保护 | **Dry-Run 演习模式** |
| 用户界面 | one-shot | **REPL 交互模式**（/help /session /reset /exit） |

---

## 2. 架构全景（M1+M2）

```
python -m agent_runtime "fix the bug"
        │
        ├── one-shot: cli.py → Agent.ask()
        └── REPL:     cli.py → 交互循环 → Agent.ask()
                │
                ▼
┌──────────────────────────────────────────────────┐
│              AgentLoop (agent_loop.py)            │
│                                                   │
│  while True:                                      │
│    prompt = ContextManager.build(user_msg)        │
│            │                                      │
│            ├── prefix  (~2000t) ─ System Prompt  │
│            ├── memory  (~800t)  ─ 工作记忆 (M3)  │
│            ├── relevant(~600t) ─ 记忆检索 (M3)   │
│            ├── history (~2600t) ─ 压缩历史       │
│            └── request (不限)   ─ 用户输入       │
│                                                   │
│    cache_key = prefix.hash                        │
│    raw = client.complete(prompt, cache_key)       │
│    kind, payload = Agent.parse(raw)               │
│                                                   │
│    if tool:                                       │
│      result = ToolExecutor.execute(name, args)    │
│              │                                    │
│              ├─ ① allowed_tools 白名单            │
│              ├─ ② 工具存在                        │
│              ├─ ③ 参数校验 (auto_validate)        │
│              ├─ ④ 重复调用 (最近2次相同→拒绝)     │
│              ├─ [DRY-RUN? → 返回计划，跳过执行]   │
│              ├─ ⑤ 审批 (auto/ask/never)          │
│              ├─ ⑥ 执行前 SHA256 快照              │
│              ├─ ⑦ 执行工具                        │
│              └─ ⑧ 执行后快照 → affected_paths    │
│                                                   │
│      record(result) → 继续循环                    │
│    if final:                                      │
│      return answer                                │
└──────────────────────────────────────────────────┘
```

---

## 3. 新增模块地图（4 个文件）

### 3.1 工具执行闸口

**`tool_executor.py`** (247 行)

7 道检查，每道失败返回 `ToolExecutionResult(content, metadata)`，绝不抛异常。

```
1. allowed_tools  ──→ rejected: "allowed_tools"
2. 工具存在       ──→ rejected: "not_found"
3. 参数校验       ──→ rejected: "invalid_args"
4. 重复调用       ──→ rejected: "duplicate"    (最近2次 name+args 完全相同)
5. [DRY-RUN]      ──→ success: "dry_run"       (返回计划，跳过后续)
6. 审批           ──→ rejected: "approval_denied"
7. 执行前快照     ──→ 高风险工具 SHA256 全文件
8. 执行           ──→ 调用 tool_spec["run"]
9. 执行后快照     ──→ affected_paths + diff_summary
```

**关键类**：
- `ToolExecutionResult(content, metadata)` — 无论成功/失败都返回此结构
- `ToolExecutor(agent, approval_policy, dry_run)` — 8 步执行管线
- `_capture_snapshot()` → `{rel_path: sha256}` 全仓库快照
- `_is_duplicate(name, args)` — 从 session history 取最近 2 次带 `tool_name` 的记录

### 3.2 Token 预算管理

**`context_manager.py`** (208 行)

用 `tiktoken` 替代 M1 的字符数估算，实现精确 token 计数和自动裁剪。

**`TokenBudget` 类**：
```python
budget = TokenBudget(model="gpt-4", total_limit=6000)
budget.count("你好世界")   # → 5 tokens（精确）
budget.fit(text, 2600)     # → 截断到 2600 tokens 以内
```

**`ContextManager` 类**：
```python
cm = ContextManager(agent)
prompt, metadata = cm.build(user_message)
# metadata = {
#     "sections": {"prefix": 1800, "memory": 0, "relevant": 0, "history": 500, "request": 50},
#     "total_tokens": 2350,
#     "cuts": [],                           # 裁剪日志
#     "prompt_cache_key": "abc123...",       # prefix SHA256
# }
```

**裁剪优先级**：`relevant → history → memory → prefix`（request 永不被裁）

### 3.3 安全模块

**`security.py`** (90 行)

```python
# L1: Shell 环境变量白名单
shell_env(root="/workspace")  # → {"HOME": "...", "PATH": "...", "PWD": "/workspace"}

# L2: 敏感变量名检测
looks_sensitive_env_name("DEEPSEEK_API_KEY")  # → True
looks_sensitive_env_name("HOME")              # → False

# L3: 文本脱敏
redact_text("api key: sk-1234", secret_values=["sk-1234"])  # → "api key: <redacted>"
```

### 3.4 CLI REPL

**`cli.py`** (+92 行)

```bash
python -m agent_runtime              # → REPL 模式
python -m agent_runtime "prompt"     # → one-shot
python -m agent_runtime --dry-run    # → 演习模式
```

内置命令：`/help` `/memory` `/session` `/reset` `/exit`

---

## 4. 关键设计决策

### 4.1 为什么闸口失败不抛异常？

7 道闸口任意一道失败都返回 `ToolExecutionResult`，而不是抛异常。原因：

- **Agent 需要感知错误**：抛异常会导致 AgentLoop 无法区分"工具拒绝"和"系统崩溃"，而 `ToolExecutionResult` 的错误信息被写入 history，模型可以调整策略重新尝试
- **结构化 metadata**：`tool_error_code` 让后期分析知道"哪个闸口拒绝了多少次"，M7 消融实验会用到这些数据

### 4.2 为什么 Dry-Run 放在审批之前？

Dry-Run 意味着不修改文件，不修改文件意味着不需要审批。这保证了 `--dry-run --approval never` 也能正常工作——演习不应该被审批策略阻挡。

### 4.3 为什么重复调用只看最近 2 次？

一个轻量但有效的死循环检测。如果 Agent 连续 3 次调用同一个工具且参数完全相同，第 3 次被拦截。这种模式最容易出现在"模型拿到了不符合预期的结果，但坚持用同样的参数重试"。

### 4.4 为什么用 tiktoken 而不是继续用字符数？

LLM 按 token 计费和限制上下文。M1 用字符数估算在中文场景误差可达 3-5 倍（"你好世界" = 4 字符 ≠ 5 tokens，但一段中文 prompt 的 token 数通常是字符数的 1.5-2 倍）。tiktoken 是 OpenAI 开源的 Rust 内核的 Python 绑定，仅 ~3MB，误差 <5%。

### 4.5 为什么 ContextManager 的裁剪优先级是 relevant → history → memory → prefix？

- **relevant** 最先裁：记忆检索结果是"可能有帮助的参考"，不是必须的
- **history** 次之：历史对话可以被压缩或丢弃，不影响当前推理
- **memory** 第三：工作记忆比历史更重要（包含关键文件和决策）
- **prefix** 最后：System Prompt 中的规则和工具描述是 Agent 正确行为的基础
- **request** 永不裁：用户输入是 Agent 存在的全部意义

### 4.6 为什么 Prompt Cache key 是 prefix SHA256 而不是整个 prompt？

Anthropic Cache 要求标记"重复出现的文本块"。prefix（System Prompt + 工具列表 + Workspace 快照）在一次会话中几乎不变，是理想的缓存对象。history 每轮都在变，缓存无意义。Workspace 快照变了（切换分支/修改文件），hash 自动改变 → cache 自动失效。

---

## 5. 工具矩阵（6 个）

| 工具 | 参数 | 风险 | 快照 | 特性 |
|------|------|:--:|:--:|------|
| `list_files` | path(默认.) | ✓ | — | 过滤 IGNORED_PATH_NAMES |
| `read_file` | path, start, end | ✓ | — | 行号前缀输出 |
| `search` | pattern, path | ✓ | — | rg → Python fallback |
| `write_file` | path, content | ⚠ | SHA256 | 自动 mkdir(parents=True) |
| `patch_file` | path, old, new | ⚠ | SHA256 | old_text 必须恰好 1 次 |
| `run_shell` | command, timeout | ⚠ | SHA256 | env 白名单，1-120s |

---

## 6. 数据流：一次高风险工具调用的完整路径

```python
# AgentLoop 收到 tool 指令
tool_name = "write_file"
tool_args = {"path": "fix.py", "content": "x=1"}

# → Agent.execute_tool()
executor = ToolExecutor(agent, approval_policy="ask", dry_run=False)
result = executor.execute("write_file", {"path": "fix.py", "content": "x=1"})

# Gate 1: "write_file" in allowed_tools? ✅
# Gate 2: registry["write_file"] exists? ✅
# Gate 3: auto_validate(WriteFileArgs, {"path":"fix.py","content":"x=1"}) ✅
# Gate 4: recent 2 calls same name+args? ❌ (first call)
# DRY-RUN: False → 继续
# Gate 5: high_risk + approval_policy="ask" → input("approve?") → user says "y"
# Gate 6: SHA256 snapshot of all files
# Gate 7: tools["write_file"]["run"](args) → target.write_text("x=1")
# Gate 8: SHA256 snapshot again → diff = {affected_paths: ["fix.py"], diff_summary: "+  fix.py"}
# → ToolExecutionResult(content="已写入 fix.py（3 字符）", metadata={"tool_status":"success","affected_paths":["fix.py"]})
```

---

## 7. 测试策略

```
tests/
├── test_tool_executor.py    (12 tests)  # 闸口: 白名单/参数/重复/审批/快照
├── test_context_manager.py  (13 tests)  # Token计数/裁剪/压缩/中文
├── test_shell_security.py   (6 tests)   # shell_env/redact/sensitive
├── test_write_patch.py      (8 tests)   # write/patch 正常+异常
├── test_cache_and_dryrun.py (7 tests)   # cache key/dry-run 无副作用
├── test_integration.py      (6 tests)   # 完整管线 (FakeClient)
└── (M1 tests)               (68 tests)  # 回归验证

总计: 120 tests
```

---

## 8. 快速上手

```bash
# REPL 模式
python -m agent_runtime

# Dry-Run 演习（不修改文件）
python -m agent_runtime --dry-run "create a config file"

# 自动批准高风险工具
python -m agent_runtime --approval auto "fix the type error in calc.py"

# 查看会话状态
/session        # → 会话 ID / 轮数 / approval / dry_run
/reset          # → 清空历史
/exit           # → 退出
```

---

*M2 完成日期：2026-06-29 | git tag: m2-done | 120 tests green | 3575 total LOC*
