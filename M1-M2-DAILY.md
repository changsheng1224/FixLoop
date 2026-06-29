# M1-M2 每日开发计划（Week 1-4）

> 每天约 4-6 小时有效编码时间。带 ⚡ 标记的任务是当日核心产出，必须完成。带 🔧 标记的是可顺延到次日或周末补的辅助任务。

---

## M1：Agent 运行时内核 — 控制循环与工具系统（Week 1-2）

**目标：从空目录开始，写出一个能跑的最小 Agent。输入一句话，模型能调用只读工具、读文件、返回答案。**

### 前置准备（Week 1 开始前）

- [ ] 安装 Python 3.11+
- [ ] `mkdir agent_runtime && cd agent_runtime`
- [ ] `git init && git commit --allow-empty -m "init"`
- [ ] 注册 DeepSeek API Key（`https://platform.deepseek.com`），充值 ¥10
- [ ] 创建 `.env` 文件，填入 `DEEPSEEK_API_KEY`

---

### Day 1（周一）：项目骨架 + Config 系统

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:00 | ⚡ 创建项目目录结构 + `pyproject.toml`（`[project] name="agent_runtime"`, `requires-python=">=3.11"`, `[dependency-groups] dev=["pytest","ruff"]`） | 项目可 `pip install -e .` |
| 10:00-11:30 | ⚡ 实现 `config.py`：`AgentConfig(BaseModel)` — 字段 `provider: str="deepseek"`, `model: str="deepseek-v4-pro"`, `max_steps: int=6`, `max_new_tokens: int=512`, `approval: str="ask"`, `temperature: float=0.2`。`from_env_and_args(env_path, args)` 从 `.env` 加载 + CLI args 覆盖 + 启动时 `model_validate()` | `AgentConfig(provider="invalid")` → `ValidationError` |
| 11:30-12:00 | 🔧 写 3 个 config 单测：正常加载、缺少必填字段报错、环境变量覆盖默认值 | `tests/test_config.py` 3 tests green |
| 14:00-15:30 | ⚡ 实现 `workspace.py`：`WorkspaceContext.build(cwd)` — `git rev-parse --show-toplevel` 找 repo_root，`git branch --show-current`，`git status --short`，`git log --oneline -5`。`DOC_NAMES = ("AGENTS.md", "README.md", "pyproject.toml")` 白名单预加载。`text()` 方法拼成 `Workspace:\n- cwd: ...\n- branch: ...` 格式。`fingerprint()` 返回 SHA256 | `print(WorkspaceContext.build(".").text())` 正常输出 |
| 15:30-16:30 | 🔧 写 workspace 单测：正常 git 仓库、非 git 目录降级、白名单文档加载 | `tests/test_workspace.py` 2 tests green |
| 16:30-17:30 | 复盘 + git commit | 今天约 250 行代码 |

**Day 1 验收：** `python -c "from agent_runtime.config import AgentConfig; print(AgentConfig(provider='deepseek'))"` 正常；`python -c "from agent_runtime.workspace import WorkspaceContext; print(WorkspaceContext.build('.').fingerprint())"` 输出 64 位 hex。

---

### Day 2（周二）：FakeClient + 模型输出解析

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:00 | ⚡ 实现 `providers/clients.py`：`FakeModelClient` — `__init__(outputs: list[str])` 预设输出序列，`complete(prompt, max_tokens)` 依次弹出。`supports_prompt_cache = False`。`prompts: list[str]` 记录所有收到的 prompt | FakeClient(["hello"]) 第一次调用返回 "hello"，第二次抛 RuntimeError |
| 10:00-11:30 | ⚡ 实现 `runtime.py`：`Agent` 类骨架。`__init__(model_client, workspace, tools, max_steps, approval_policy)`。核心方法 `parse(raw: str) -> tuple[str, dict|str]`：检测 `<tool>JSON</tool>` → `("tool", {"name":"x","args":{...}})`；检测 `<tool name="x" attrs>body</tool>` → XML 属性解析；检测 `<final>text</final>` → `("final", text)`；都不匹配 → `("retry", notice)`。`retry_notice(problem)` 返回纠错提示文本 | parse 单测：4 种输入各 1 个 test |
| 11:30-12:00 | 🔧 写 parse 单测：有效 JSON tool、有效 XML tool（含 `<content>` 子标签）、有效 final、空输入、格式错误。至少 5 tests | `tests/test_parse.py` 5 tests green |
| 14:00-15:30 | ⚡ 实现 `providers/clients.py`：`AnthropicCompatibleModelClient` — `__init__(model, base_url, api_key, temperature, timeout)`。`complete(prompt, max_new_tokens)` → `urllib.request.Request(base_url + "/v1/messages", data=json.dumps({model, messages:[{role:"user", content:[{type:"text", text:prompt}]}], max_tokens})` → `headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"}` → `urlopen` → `json.loads` → `_extract_text(data)` 遍历 `content` 找 `type=="text"` 的 `text`。含 HTTP 500 重试 3 次逻辑 | `python -c "from agent_runtime.providers.clients import AnthropicCompatibleModelClient; c = AnthropicCompatibleModelClient(model='deepseek-v4-pro', base_url='https://api.deepseek.com/anthropic', api_key='...'); print(c.complete('hello', 100))"` 返回文本 |
| 15:30-16:30 | 🔧 写 FakeClient 单测 + AnthropicClient 集成测试（调真实 API 1 次确认连通） | 2 tests green |
| 16:30-17:30 | 复盘 + git commit | 今天约 200 行代码 |

**Day 2 验收：** `FakeClient(["<final>hello</final>"]).complete("", 100)` → `"<final>hello</final>"`。`Agent.parse("<final>done</final>")` → `("final", "done")`。真实 API 调用成功返回文本。

---

### Day 3（周三）：3 个只读工具 + 工具 Schema 自动生成

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 实现 `tools.py`：工具参数 dataclass — `ListFilesArgs(path: str=".")`, `ReadFileArgs(path: str, start: int=1, end: int=200)`, `SearchArgs(pattern: str, path: str=".")`。`auto_schema(args_cls)` 从 `get_type_hints()` 推导 schema 字符串。`auto_validate(args_cls, args)` 校验类型 + 尝试转换 | `auto_schema(ReadFileArgs)` → `{"path": "str", "start": "int=1", "end": "int=200"}` |
| 10:30-12:00 | ⚡ 实现 3 个只读工具的执行函数：`tool_list_files(context, args)` → `sorted(path.iterdir())` 过滤 `IGNORED_PATH_NAMES`，输出 `[F] path` / `[D] path` 格式。`tool_read_file(context, args)` → `path.read_text().splitlines()[start-1:end]` 逐行加行号前缀。`tool_search(context, args)` → 先尝试 `subprocess.run(["rg", "-n", "--smart-case", pattern, path])`，rg 不可用时 fallback `path.rglob("*")` 逐文件 `line.lower()` 匹配 | 3 个工具在测试目录上正确运行 |
| 12:00-12:30 | 🔧 写工具单测：list_files 正常 + 目录不存在报错，read_file 正常 + start<1 报错 + 文件不存在报错，search 命中 + 无匹配 + pattern 为空报错 | `tests/test_tools.py` 7 tests green |
| 14:00-15:00 | ⚡ 实现 `tool_context.py`：`ToolContext(root, path_resolver, shell_env_provider, depth, max_depth, spawn_delegate)`。`path(raw_path)` → `path_resolver(raw_path)` 含路径逃逸检测（`os.path.commonpath`） | ToolContext 单测：workspace 内路径通过，`../etc/passwd` 报错 |
| 15:00-16:00 | ⚡ 在 `tools.py` 中实现 `build_tool_registry(context)`：生成 `{"list_files": {schema, risky:False, description, run}, "read_file": {...}, "search": {...}}`。`legal_tool_names()` 返回可调用工具名集合 | `build_tool_registry(ctx)["search"]["run"]({"pattern":"hello"})` 正常搜索 |
| 16:00-17:00 | 🔧 写工具注册单测 + 路径逃逸单测 | `tests/test_tool_registry.py` 2 tests green |
| 17:00-17:30 | 复盘 + git commit | 今天约 250 行代码 |

**Day 3 验收：** 3 个工具均可独立调用并返回正确结果。`auto_schema` 生成的 schema 与手写一致。路径逃逸被拦截。

---

### Day 4（周四）：System Prompt + Agent.ask() 最简版

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 实现 `prompt_prefix.py`：`build_prompt_prefix(workspace, tools)` — 组装系统提示词。结构：`You are pico, a local coding agent.\n\nRules:\n- ...\n\nTools:\n{tool_list}\n\nExamples:\n{examples}\n\n{workspace.text()}`。`tool_signature(tools)` 返回按 name 排序的 schema 的 SHA256 hash。`PromptPrefix(text, hash, workspace_fingerprint, tool_signature, built_at)` dataclass | 生成的 prefix 可读，含完整工具签名和示例 |
| 10:30-11:30 | ⚡ 扩展 `runtime.py`：`Agent` 类补全。`build_tools()` 调 `build_tool_registry`。`build_prefix()` 调 `build_prompt_prefix`。方法 `prompt(user_message)` — 拼 prefix + user_message。`record(item)` — append 到 `session["history"]`。`ask(user_message)` — 最简版：`record({"role":"user", ...})` → `prompt(user_message)` → `model_client.complete()` → `parse()` → 如果是 tool 则执行并 record → 如果是 final 则返回。**暂不做 max_steps 循环，只支持 1 步** | `agent.ask("read README.md")` 返回 read_file 的执行结果 |
| 11:30-12:00 | 🔧 用 FakeClient(["<tool>{\"name\":\"read_file\",\"args\":{\"path\":\"README.md\"}}</tool>", "<final>done</final>"]) 测试 ask() | `tests/test_agent.py` 1 test green |
| 14:00-15:30 | ⚡ 实现 `agent_loop.py`：`AgentLoop` 类。`run(user_message)` — while 循环版本：`tool_steps < max_steps and attempts < max_steps*3+4` → build prompt → complete → parse → tool 则 execute + record + continue → retry 则 record + continue → final 则 record + return。`attempts` 和 `tool_steps` 分开计数 | FakeClient 预设 [tool1, tool2, final] 序列，验证循环走 2 步工具后返回 final |
| 15:30-16:30 | ⚡ 实现 `cli.py`：`main()` — `argparse` 解析 `--cwd` / `--provider` / `--model` / `--max-steps` / `--temperature`。`build_agent(args)` 装配 Config + Workspace + Client + Agent。one-shot 模式：`python -m agent_runtime "prompt"` | 用 FakeClient 跑通 `python -m agent_runtime "hello"` |
| 16:30-17:30 | 🔧 AgentLoop 单测：单步 tool、多步 tool、tool 达到 max_steps 截断、retry 后继续、final 返回 | `tests/test_agent_loop.py` 4 tests green |

**Day 4 验收：** `python -m agent_runtime "read README.md"`（FakeClient 模式）走通完整 ask 流程。AgentLoop 正确统计 tool_steps 并在超限时停机。

---

### Day 5（周五）：联调 + 真实 API 测试 + M1 收尾

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:00 | ⚡ 用真实 DeepSeek API 跑通一次完整的 `ask()`。Prompt："read the README.md file and tell me what this project is about" | 观察 Agent 是否正确调用 read_file 并返回摘要 |
| 10:00-11:30 | ⚡ 用真实 API 跑 3 个场景：① "list all files" → 预期调 list_files ② "search for 'Agent' in the codebase" → 预期调 search ③ "what's in README.md?" → 预期调 read_file | 记录每个场景的 tool_steps、attempts、模型输出格式是否符合预期 |
| 11:30-12:00 | 🔧 修复真实 API 测试中发现的问题（Prompt 不够清晰、模型输出格式不稳定等） | Prompt 调优 |
| 14:00-15:00 | ⚡ 补全 M1 缺失的细节：① `.env.example` 模板 ② `.gitignore`（`__pycache__/`、`.env`、`.agent/`、`*.egg-info/`）③ `__init__.py` 导出公开 API ④ `__main__.py`（`from .cli import main; sys.exit(main())`） | 项目结构完整 |
| 15:00-16:00 | 🔧 补充集成测试：用 FakeClient 模拟一次完整的多步工具调用 | `tests/test_integration.py` 1 test green |
| 16:00-17:00 | ⚡ M1 复盘：① 跑全部测试确认绿色 ② 填写 M1 验收清单 ③ git tag m1-done | M1 正式完成 |
| 17:00-17:30 | 规划 M2 内容 | |

**Day 5 验收（M1 里程碑）：**
- [ ] `python -m agent_runtime "read the README"` 调真实 API 成功响应
- [ ] 3 个只读工具均可被 Agent 正确调用
- [ ] AgentLoop 正确控制步数和停机
- [ ] `pytest tests/ -v` 全部绿色（约 20 tests）
- [ ] Config pydantic 校验生效
- [ ] 代码量约 500 行

---

### M1 周末缓冲（可选）

如果 Day 1-5 有未完成的任务：
- 周六上午：补未完成的单测
- 周六下午：补充代码注释（中文 docstring）
- 周日：休息

---

## M2：完整工具系统 + Token 预算 + Dry-Run（Week 3-4）

**目标：补全高风险工具 + 7 道闸口 + tiktoken 精确预算 + Dry-Run + REPL 模式。**

---

### Day 6（周一）：高风险工具实现

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 实现 `write_file` 工具：`WriteFileArgs(path: str, content: str)`。执行：`path.parent.mkdir(parents=True, exist_ok=True)` → `path.write_text(content, encoding="utf-8")` → return `f"wrote {path} ({len(content)} chars)"` | `write_file("test.txt", "hello")` 创建文件并返回确认 |
| 10:30-12:00 | ⚡ 实现 `patch_file` 工具：`PatchFileArgs(path: str, old_text: str, new_text: str)`。执行：`text = path.read_text()` → `count = text.count(old_text)` → `if count != 1: raise ValueError(f"must occur exactly once, found {count}")` → `path.write_text(text.replace(old_text, new_text, 1))` | `old_text` 出现 0 次 → 报错；出现 2 次 → 报错；精确 1 次 → 替换成功 |
| 12:00-12:30 | 🔧 写 write_file + patch_file 单测：正常写入、覆盖已有文件、patch 精确命中 1 次、patch 命中 0 次报错、patch 命中多次报错 | `tests/test_write_patch.py` 5 tests green |
| 14:00-15:30 | ⚡ 实现 `run_shell` 工具：`RunShellArgs(command: str, timeout: int=20)`。执行：`subprocess.run(command, cwd=root, shell=True, capture_output=True, text=True, timeout=timeout, env=shell_env())` → 格式化输出 `exit_code: {}\nstdout:\n{}\nstderr:\n{}` | timeout 限制生效；env 白名单生效 |
| 15:30-16:30 | ⚡ 实现 `security.py`：`shell_env(allowlist, root)` 只透传 HOME/PATH/PWD 等安全变量并覆盖 PWD 为 root。`looks_sensitive_env_name(name)` 检测 API_KEY/TOKEN/SECRET/PASSWORD。`redact_text(text, secret_env_names)` 将敏感值替换为 `<redacted>` | `run_shell("env")` 输出不含 DEEPSEEK_API_KEY |
| 16:30-17:30 | 🔧 写 run_shell 单测 + security 单测：正常命令、超时终止、敏感变量过滤、文本脱敏 | `tests/test_shell.py` + `tests/test_security.py` 共 4 tests green |

**Day 6 验收：** 6 个基础工具全部实现。`run_shell("echo $DEEPSEEK_API_KEY")` 输出 `<redacted>` 或空。

---

### Day 7（周二）：工具执行闸口（7 道检查）

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 实现 `tool_executor.py`：`ToolExecutionResult(content, metadata)` dataclass。`ToolExecutor(agent)` 类。`execute(name, args)` 方法，按序执行 7 道检查：① allowed_tools 白名单 → 不在则返回 rejected ② 工具存在 → 不存在则返回 rejected ③ `agent.validate_tool(name, args)` → 异常则返回 rejected（含 example 提示） | 前三道闸口就位 |
| 10:30-11:30 | ⚡ 续：④ `repeated_tool_call(name, args)` → 最近 2 次 history 中 tool 事件的 name+args 完全相同则返回 rejected ⑤ 高风险工具 + `agent.approve(name, args)` → 用户拒绝则返回 rejected ⑥ 高风险工具执行前 `capture_workspace_snapshot()`（遍历 root 下所有文件 SHA256） ⑦ 执行工具 ⑧ 高风险工具执行后再次快照 + `diff_workspace_snapshots(before, after)` 生成 affected_paths + diff_summary | 7 道闸口就位 |
| 11:30-12:00 | 🔧 写 tool_executor 单测：allowed_tools 拒绝、unknown tool 拒绝、参数非法拒绝（含路径逃逸）、重复调用拒绝、审批拒绝、快照对比正确 | `tests/test_tool_executor.py` 6 tests green |
| 14:00-15:30 | ⚡ 将 ToolExecutor 集成到 Agent：`agent.tool_executor = ToolExecutor(agent)`。`Agent.execute_tool(name, args)` → `tool_executor.execute(name, args)`。执行后自动调 `update_memory_after_tool()`（暂不实现 memory 存储，只预留钩子）。`capture_workspace_snapshot()` 返回 `{relative_path: sha256}` 字典 | Agent 调用工具的入口统一走 ToolExecutor |
| 15:30-16:30 | ⚡ 在 Agent 中实现 `approve(name, args)`：`approval_policy=="auto"` → True；`"never"` → False；`"ask"` → `input(f"approve {name}? [y/N] ")` | REPL 中调用 write_file 触发审批提示 |
| 16:30-17:30 | 🔧 写 Agent + ToolExecutor 集成测试 | `tests/test_agent_tools.py` 2 tests green |

**Day 7 验收：** 所有工具调用经过 ToolExecutor 7 道闸口。高风险工具触发审批。重复调用被拦截。

---

### Day 8（周三）：Token 精确预算 + ContextManager

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:00 | ⚡ `pip install tiktoken`，实现 `TokenBudget` 类：`__init__(model, total_limit=6000)` → `self.encoder = tiktoken.encoding_for_model(model)`。`count(text)` → `len(self.encoder.encode(text))`。`fit(text, limit)` → 超限则截断 decode 回文本 | `TokenBudget("gpt-4").count("你好世界")` 返回精确 token 数 |
| 10:00-12:00 | ⚡ 实现 `context_manager.py`：`ContextManager(agent, total_budget=6000)`。5 个 section 预算：prefix(2000) / memory(800) / relevant(600) / history(2600) / request(不限制)。`build(user_message)` → 收集各 section 文本 → 组装 prompt → 超预算按 `relevant → history → memory → prefix` 顺序逐段裁剪（每次减 100 tokens 直到达标）。返回 `(prompt_text, metadata_dict)`。metadata 含各 section 的 raw_chars / budget_tokens / rendered_tokens / 裁剪日志 | 构造超长 history（50 轮），验证裁剪从 relevant 开始、最后才动 prefix |
| 12:00-12:30 | 🔧 写 ContextManager 单测：正常组装、超预算裁剪 relevant、超预算裁剪 history、超预算裁剪 memory、中文 token 计数准确、request section 永不被裁剪 | `tests/test_context_manager.py` 6 tests green |
| 14:00-15:30 | ⚡ 实现历史智能压缩：`_compressed_history_entries(history)` — 最近 6 条完整保留（900 chars/条），更早的：① 重复 read_file 合并为一行 `path -> summary` ② 旧工具结果压缩为单行摘要 ③ 旧 user/assistant 消息截断到 60 chars。`_render_history_section(budget)` — 从最新到最旧逐条填充，budget 用尽停 | 50 轮历史压缩后 < 2600 tokens |
| 15:30-16:30 | ⚡ 将 ContextManager 集成到 Agent：`agent.context_manager = ContextManager(agent)`。`agent._build_prompt_and_metadata(user_message)` 调 `context_manager.build()` | `agent.ask("hello")` 使用 ContextManager 组装 prompt |
| 16:30-17:30 | 🔧 写历史压缩单测 + 集成测试 | `tests/test_history_compression.py` 2 tests green |

**Day 8 验收：** Token 计数精确（与 OpenAI tokenizer 页面结果一致）。超预算自动裁剪且不影响用户请求。历史压缩后 token 数显著下降。

---

### Day 9（周四）：Prompt Cache + Dry-Run + 自动 Schema 完善

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ Prompt Cache 支持：`prompt_prefix.py` — `PromptPrefix.hash` 为 prefix_text 的 SHA256。`context_manager.py` — metadata 中输出 `prompt_cache_key=prefix_hash`。`providers/clients.py` — AnthropicCompatibleClient 的 `complete()` 接受 `prompt_cache_key` 参数并透传到 payload（当 `supports_prompt_cache=True` 时） | 两次 ask 间工作区未变 → cache key 相同 → 后端命中缓存（通过 API 返回的 usage.cached_tokens > 0 确认） |
| 10:30-12:00 | ⚡ Dry-Run 模式：扩展 `tools.py` — 每个工具的执行函数接受 `dry_run: bool=False`。dry_run=True 时不执行实际操作，返回 `[DRY RUN] Would {tool_name}({args})`。Agent 增加 `dry_run` 属性，透传给 ToolExecutor。CLI 增加 `--dry-run` 全局开关 | `python -m agent_runtime --dry-run "delete temp files"` → Agent 规划所有工具调用 → 全部输出 [DRY RUN] → 不实际修改文件 |
| 12:00-12:30 | 🔧 写 Prompt Cache 单测 + Dry-Run 单测 | 各 1 test green |
| 14:00-15:00 | ⚡ 完善 auto_schema：将 M1 的工具参数 dataclass 与 auto_schema/auto_validate 生成器整理为独立模块 `schema_utils.py`。新增 `tool_example(name)` 函数从 `TOOL_EXAMPLES` 字典返回标准化示例 | `auto_schema(WriteFileArgs)` → `{"path": "str", "content": "str"}` |
| 15:00-16:30 | ⚡ CLI REPL 模式完善：`cli.py` — `main()` 无参数时进入交互循环。内置命令：`/help`（打印帮助）、`/memory`（打印工作记忆，暂时返回空）、`/session`（打印 session id）、`/reset`（清空 history）、`/exit`。每次输入交给 `agent.ask(user_input)` 处理 | 多轮对话中 history 累积，后续轮次 Agent 能看到之前的上下文 |
| 16:30-17:30 | 🔧 写 REPL 集成测试（用 FakeClient） | `tests/test_cli.py` 1 test green |

**Day 9 验收：** Cache key 正确生成和透传。`--dry-run` 全局生效。REPL 多轮对话正常。

---

### Day 10（周五）：联调 + 集成测试 + M2 收尾

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 真实 API 全流程测试：用 DeepSeek API 跑一遍完整的多步工具调用（read_file → search → write_file → run_shell），观察 Dry-Run 和实际执行。验证 Prompt Cache 是否命中（查看 API 返回的 cached_tokens） | 记录 token 消耗和 cache 命中情况 |
| 10:30-12:00 | ⚡ 真实 API 测试审批流程：`--approval ask` 模式下跑 write_file 验证弹窗提示。`--approval auto` 模式下跳过审批。`--approval never` 模式下拒绝写入 | 三种审批模式按预期工作 |
| 12:00-12:30 | 🔧 修复真实 API 测试中发现的 Prompt 格式问题 | Prompt 微调 |
| 14:00-15:30 | ⚡ 补充和整理所有测试：确认 M1 + M2 的所有单测和集成测试通过。目标 40+ tests | `pytest tests/ -v` 全绿 |
| 15:30-16:30 | ⚡ 代码整理：`ruff check` + `ruff format`。补全所有公开函数的 docstring（中文，Google style） | CI 风格零 warning |
| 16:30-17:30 | ⚡ M2 复盘 + M1-M2 总结。git tag m2-done。统计代码量（目标 ~1000 行） | M2 正式完成 |

**Day 10 验收（M2 里程碑）：**
- [ ] 6 个工具全部可用（含 write/patch/shell）
- [ ] 7 道闸口全部生效
- [ ] Token 预算精确裁剪
- [ ] 历史智能压缩
- [ ] Prompt Cache 透传
- [ ] Dry-Run 模式
- [ ] REPL 多轮交互
- [ ] `pytest tests/ -v` 全部绿色（40+ tests）
- [ ] `ruff check` 零 warning
- [ ] 代码量约 1000 行（M1 500 + M2 500）

---

## 附录：M1-M2 文件清单

```
agent_runtime/
├── __init__.py              # M1 Day5
├── __main__.py              # M1 Day5
├── cli.py                   # M1 Day4 + M2 Day9
├── config.py                # M1 Day1
├── workspace.py             # M1 Day1
├── schema_utils.py          # M1 Day3 + M2 Day9
├── tools.py                 # M1 Day3 + M2 Day6
├── tool_context.py          # M1 Day3
├── tool_executor.py         # M2 Day7
├── runtime.py               # M1 Day2 + M1 Day4
├── agent_loop.py            # M1 Day4
├── prompt_prefix.py         # M1 Day4 + M2 Day9
├── context_manager.py       # M2 Day8
├── security.py              # M2 Day6
└── providers/
    └── clients.py           # M1 Day2 + M2 Day9

tests/
├── test_config.py           # M1 Day1
├── test_workspace.py        # M1 Day1
├── test_parse.py            # M1 Day2
├── test_tools.py            # M1 Day3
├── test_tool_registry.py    # M1 Day3
├── test_agent.py            # M1 Day4
├── test_agent_loop.py       # M1 Day4
├── test_integration.py      # M1 Day5
├── test_write_patch.py      # M2 Day6
├── test_shell.py            # M2 Day6
├── test_security.py         # M2 Day6
├── test_tool_executor.py    # M2 Day7
├── test_agent_tools.py      # M2 Day7
├── test_context_manager.py  # M2 Day8
├── test_history_compression.py # M2 Day8
├── test_cli.py              # M2 Day9
└── conftest.py              # M1 Day1（共享 fixtures：FakeClient, temp workspace）
```

## 附录：每日时间分配原则

| 时段 | 活动 | 占比 |
|------|------|:--:|
| 09:00-12:00 | ⚡ 核心代码产出 | 50% |
| 12:00-12:30 | 🔧 单测 | 10% |
| 14:00-16:30 | ⚡ 核心代码产出 | 40% |
| 16:30-17:30 | 🔧 单测/复盘/commit | 可弹性 |

- **上午产出下午测试，还是边写边测？** → 一个模块写完后立即写测试（不要攒到一天结束）。测试通过后再进入下一个模块
- **卡住超过 30 分钟怎么办？** → 标记 TODO，先跳过，晚上复盘时回来解决
- **每天开始前 10 分钟** → review 昨天代码 + 跑一遍测试确认绿色
