# M1 GUIDE — Agent 运行时内核

> 读完本文你将理解：从 `urllib` 发 HTTP 请求开始，一个完整的 LLM Agent 运行时内核是怎么从零构建的。

---

## 1. 一句话定位

**从 Python 标准库（`urllib`/`subprocess`/`json`/`ast`）零 LLM 框架依赖，手写 ~1400 行 Agent 运行时内核。**

核心闭环：用户输入 → 组装 System Prompt → 调模型 → 解析输出 → 执行工具 → 记录结果 → 循环直到拿到最终答案。

---

## 2. 架构全景

```
python -m agent_runtime "read the README"
        │
        ▼
┌──────────────────────────────────────────────┐
│                 cli.py                        │
│  argparse → _load_dotenv() → Config          │
│  → WorkspaceContext → ModelClient            │
│  → Agent.ask("read the README")              │
└───────────────┬──────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────┐
│              Agent (runtime.py)               │
│                                               │
│  .ask(user_message)                           │
│    └→ AgentLoop.run(user_message)            │
│         │                                     │
│         ├→ .prompt(user_message)             │
│         │    = prefix.text + history + task  │
│         │                                     │
│         ├→ model_client.complete(prompt)     │
│         │    (urllib.request → API)          │
│         │                                     │
│         ├→ .parse(raw)                       │
│         │    → "tool" / "final" / "retry"   │
│         │                                     │
│         ├→ .execute_tool(name, args)         │
│         │    └→ registry[name]["run"](args) │
│         │                                     │
│         └→ .record(item) → session.history   │
│                                               │
│  停机: final / tool_steps>max_steps          │
└──────────────────────────────────────────────┘
```

---

## 3. 模块地图（12 个文件）

### 3.1 配置与工作区

| 模块 | 职责 | 关键细节 |
|------|------|------|
| `config.py` (33行) | `AgentConfig(BaseModel)` | provider/model/max_steps/temperature/approval 7 字段，ge/le 约束，启动时 `model_validate()` |
| `workspace.py` (155行) | `WorkspaceContext` | `git rev-parse` 找 repo_root → `git branch` → `git status --short` → `git log -5` → 白名单文档加载（AGENTS.md/README.md/pyproject.toml/CLAUDE.md） |

### 3.2 模型后端

| 模块 | 职责 | 关键细节 |
|------|------|------|
| `providers/clients.py` (159行) | `FakeModelClient` + `AnthropicCompatibleModelClient` | FakeClient 预设输出序列用于测试；AnthropicClient 用 `urllib.request` 发 POST，3 次重试（仅 5xx），`_extract_text()` 兼容 Anthropic/OpenAI/DeepSeek thinking tokens 三种格式 |

### 3.3 工具系统

| 模块 | 职责 | 关键细节 |
|------|------|------|
| `schema_utils.py` (116行) | `auto_schema()` + `auto_validate()` | 从 dataclass type hints 自动推导 `{"path": "str", "start": "int=1"}` 格式 schema，自动类型转换（`"10"`→`int`） |
| `tool_context.py` (63行) | `ToolContext` | `resolve(raw_path)` → `os.path.commonpath` 路径逃逸检测，拦截 `../etc/passwd` |
| `tools.py` (299行) | 6 dataclass + 3 执行函数 + registry | `list_files`（过滤 IGNORED_PATH_NAMES）、`read_file`（行号前缀输出）、`search`（rg → Python fallback） |

### 3.4 Agent 核心

| 模块 | 职责 | 关键细节 |
|------|------|------|
| `prompt_prefix.py` (136行) | System Prompt 构建器 | Persona → 7 规则 → 工具列表（✓安全/⚠高风险）→ 4 调用示例 → Workspace 快照。`PromptPrefix` dataclass 含 SHA256 hash（prompt cache 用） |
| `runtime.py` (192行) | `Agent` 类 | session 管理、`prompt()` 组装、`record()` 记录、`ask()` 调用 AgentLoop、`parse()` 支持 JSON tool / XML tool / final 三种格式 |
| `agent_loop.py` (99行) | `AgentLoop` 控制循环 | while 循环，两个停机条件：`tool_steps > max_steps` / `attempts >= max_steps*3+4`。retry 时 `attempts+1` 但不增加 `tool_steps` |

### 3.5 入口

| 模块 | 职责 | 关键细节 |
|------|------|------|
| `cli.py` (115行) | CLI 装配 | argparse → `_load_dotenv()` → Config → Workspace → ModelClient → Agent → `ask()` |

---

## 4. 数据流：一次 ask() 的完整过程

```python
# 1. 用户调用
agent.ask("what does config.py do?")

# 2. Agent.ask() 创建 AgentLoop 实例
loop = AgentLoop(agent=self)

# 3. loop.run() 记录用户输入
agent.record({"role": "user", "content": "what does config.py do?"})

# 4. 进入循环
while True:
    # 4a. 组装完整 prompt
    prompt = agent.prompt(user_message)
    # = prefix.text (系统提示词 + 工具 + 示例 + Workspace)
    # + history (对话历史，截断过长的工具输出)
    # + "## 当前任务\n\nwhat does config.py do?"

    # 4b. 用 urllib 调模型
    raw = agent.model_client.complete(prompt, max_new_tokens=512)
    # → POST https://api.deepseek.com/anthropic/v1/messages
    # → 返回 '<tool>{"name":"read_file","args":{"path":"agent_runtime/config.py"}}</tool>'

    # 4c. 解析模型输出
    kind, payload = agent.parse(raw)
    # → ("tool", {"name": "read_file", "args": {"path": "agent_runtime/config.py"}})

    # 4d. 执行工具
    if kind == "tool":
        agent.record({"role": "assistant", "content": "调用工具: read_file"})
        result = agent.execute_tool("read_file", {"path": "agent_runtime/config.py"})
        # → tool_context.resolve("agent_runtime/config.py")  # 路径逃逸检测
        # → path.read_text() → 加行号前缀 → 返回
        agent.record({"role": "tool", "content": result})
        user_message = f"工具 read_file 执行完成。\n结果:\n{result}"
        # 继续循环

    # 4e. 下一轮，模型拿到文件内容后返回最终答案
    # raw = "<final>config.py 定义了 AgentConfig 类，包含 7 个字段...</final>"
    if kind == "final":
        agent.record({"role": "assistant", "content": str(payload)})
        return str(payload)
```

---

## 5. 关键设计决策（为什么这样而不是那样）

### 5.1 为什么 `Agent.parse()` 支持两种工具格式？

**JSON 格式** `<tool>{"name":"x","args":{...}}</tool>` 适合简短调用；**XML 格式** `<tool name="x" path="f.py">body</tool>` 适合多行内容（如 `write_file` 的 content）。两种格式让模型选择最合适的表达方式。

### 5.2 为什么 `tool_steps` 和 `attempts` 是分开的？

`tool_steps` 只在实际执行工具时 +1；`attempts` 在每次调模型后 +1。格式错误时 `attempts` 增加但 `tool_steps` 不变。这防止模型因输出格式问题而耗尽可用步数。

### 5.3 为什么用 `> max_steps` 而不是 `>=`？

用 `>` 确保 `max_steps=N` 时模型可以执行 N 次工具，然后在第 N+1 次迭代中被拦下。用 `>=` 会在第 N 次工具执行后立即停机，不给模型机会生成 final answer。

### 5.4 为什么工具参数用 dataclass + auto_schema？

手写 `{"path": "str", "start": "int=1"}` 容易写错。dataclass 是唯一真相源——新增工具只需定义 dataclass + 执行函数，schema 和类型校验自动生成。

### 5.5 为什么 `ToolContext` 是独立对象而不是 Agent 的方法？

权限检查（路径逃逸）应该是基础设施层的能力，与 LLM 推理完全解耦。Agent 不能"绕过" ToolContext 的路径检测。未来 M5 的 `ToolGateway` 中间件也基于同样的设计哲学。

### 5.6 为什么 PromptPrefix 有 SHA256 hash？

相同 workspace + 相同工具集的 prefix 产生相同 hash。未来 M2 接入 Anthropic Prompt Cache 时，后端可复用缓存，减少 token 消耗。

---

## 6. 测试策略

```
tests/
├── conftest.py               # temp_workspace / non_git_dir fixtures
├── test_config.py            # 8 tests: Pydantic 校验
├── test_workspace.py         # 11 tests: git / 非git / 白名单文档 / 指纹
├── test_clients_and_parse.py # 12 tests: FakeClient + parse() 全格式
├── test_anthropic_client.py  # 5 tests: _extract_text(3) + 真实API(2, 可skip)
├── test_tools.py             # 19 tests: 工具执行 + registry + 路径逃逸
├── test_prompt_prefix.py     # 4 tests: prefix 构建 + hash 一致性
├── test_agent_loop.py        # 8 tests: 控制循环 + 停机 + retry
└── test_integration.py       # 6 tests: 完整管线（FakeClient 模拟）

总计: 73 tests, 3.3:1 测试/代码比
```

---

## 7. 快速上手

```bash
# 环境
conda activate fixloop          # Python 3.11.15
pip install -e ".[dev]"

# 运行 Agent（真实 API，需 .env 配置 DEEPSEEK_API_KEY）
python -m agent_runtime "explain what this project does"

# 运行 Agent（FakeClient，无需网络）
python -m agent_runtime --provider fake "any question"

# 全部测试
pytest tests/ -v

# 代码质量
ruff check
```

---

## 8. 面试中可以展开讲的三点

### "你的控制循环是怎么写的？"

```
感知：prompt_prefix.py 组装 System Prompt → runtime.py Agent.prompt() 拼 prefix+history+task
决策：providers/clients.py → urllib.request.urlopen() → Anthropic Messages API
行动：runtime.py Agent.parse() → 提取 tool/final/retry
      → Agent.execute_tool() → tools.py 执行函数
      → agent_loop.py 记录结果 → 继续循环或返回 final
```

### "你做了什么安全工作？"

路径锚定（`os.path.commonpath` 防逃逸）+ Shell 环境变量白名单（M2）+ 敏感信息脱敏（M3）+ Docker 容器隔离（M6）+ ToolGateway 权限中间件（M5）。

### "你怎么测试 Agent 而不花钱调 API？"

`FakeModelClient` 预设输出序列，模拟模型的 tool/final/retry 响应。73 个测试中有 71 个不调真实 API，CI 秒级通过。

---

## 9. 依赖清单

```
生产:  pydantic >= 2.0
开发:  pytest >= 8.0, ruff >= 0.5

零:    LangChain, OpenAI SDK, requests, httpx
```

---

*M1 完成日期：2026-06-29 | git tag: m1-done | 73 tests green*
