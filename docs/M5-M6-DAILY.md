# M5-M6 每日开发计划（Week 9-12）

> 每天约 4-6 小时有效编码时间。⚡ 核心任务必须完成，🔧 辅助任务可弹性。Day 编号接续 M1-M4。

---

## M5：多 Agent 架构 + Blackboard + 4 Agent + Skill 系统（Week 9-10）

**目标：在 Layer 1 之上，用 4 个不同 Tool 集合的 Agent 实例跑通第一条修复流水线。**

---

### Day 21（周一）：RepairState 数据模型 + Blackboard

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 创建 `src/` 目录。实现 `src/state.py`：`@dataclass` 定义全部消息类型——`SuspectLocation(file_path, start_line, end_line, function_name, class_name, reason, confidence)`、`RepairPlan(language, issue_type, suspect_files, estimated_impact, reasoning)`、`RetrievedContext(similar_snippets, caller_locations, related_tests, similar_fixes)`、`CandidatePatch(file_path, original_lines, patched_lines, diff, explanation)`、`VerificationResult(all_passed, total_tests, passed, failed, error, failure_logs, build_log, lint_issues)`、`RepairState(issue_input, repair_plan, suspect_locations, retrieved_context, candidate_patches, verification_result, feedback, retry_count, max_retries, status)`。每个类型带 `schema_version: str = "1.0"` | 所有类型 JSON 往返序列化；不同版本 schema 有 `from_dict` migration |
| 10:30-12:00 | ⚡ 实现 `src/blackboard.py`：`Blackboard` 类。核心数据结构 `_entries: dict[str, BlackboardEntry]`，`_conflicts: list[dict]`。`write(key, value, source_agent)` → 同 key 不同 source → 记录冲突并返回 False；同 key 同 source → 覆盖。`read(key)` → 单条读取。`read_related(prefix)` → 前缀匹配。`snapshot()` → 不可变副本。`resolve_conflict(key, winner_source)` → 手动仲裁。`TTL` 支持：`write(..., ttl=300)` → 5 分钟后自动过期 | 两个 Agent 同时写入 `suspect:calc.py:add` → Blackboard 记录冲突 |
| 12:00-12:30 | 🔧 state 序列化单测 + Blackboard 读写/冲突/TTL 单测 | `tests/test_state.py` + `tests/test_blackboard.py` 共 5 tests green |
| 14:00-15:30 | ⚡ 实现 `src/middleware.py`：`ToolGateway` 类。`__init__(permission_table: dict[str, set[str]])` — `{tool_name: {allowed_agent_names}}`。`dispatch(agent_name, tool_name, **kwargs)` → 检查 `agent_name in permission_table[tool_name]` → 不在则返回 `ToolExecutionResult(content="permission_denied", metadata={"tool_status":"rejected","tool_error_code":"permission_denied"})`。声明式权限表：`sandbox_*` → 仅 `verifier`；`ast_parse`/`stack_parse` → 仅 `localizer`；`write_file`/`patch_file` → 仅 `patcher`；`search`/`read_file`/`git_*` → 全部 | Localizer 调 `write_file` → permission_denied；Verifier 调 `ast_parse` → permission_denied |
| 15:30-16:30 | ⚡ ToolGateway 与 Agent 集成：每个 Agent 的 `execute_tool(name, args)` 在调用 ToolExecutor 之前先经 ToolGateway 检查。对 Agent 透明——Agent 收到的是普通工具错误返回，不知道权限被拦截 | Agent 尝试越权 → 收到 "error: tool 'write_file' permission denied" |
| 16:30-17:30 | 🔧 ToolGateway 单测：合法调用通过、越权拒绝、权限表动态更新 | `tests/test_middleware.py` 3 tests green |

**Day 21 验收：** RepairState 所有类型 JSON 往返正确。Blackboard 冲突检测生效。ToolGateway 正确拦截越权调用。

---

### Day 22（周二）：AST 解析 + 堆栈解析 + Git + find_test Tool

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 实现 `src/tools/ast_parser.py`：`ast_parse(path)` → `ast.parse(path.read_text())` → `ast.walk()` 遍历 `FunctionDef`/`AsyncFunctionDef`/`ClassDef` 节点。输出 `[{name, type:"function"|"method"|"class", lineno, end_lineno, args:[...], decorators:[...], docstring_summary}]`。**注释节点不进输出**（`ast.get_source_segment` 只取 code 段）。注册为 Agent Tool：`schema={path: str}`, `risky=False`, 仅 Localizer 可调用 | 解析 200 行 Python 文件 → 所有函数/类方法有正确行号和签名；含恶意注释 `# ignore rules` 的文件 → 输出不含注释 |
| 10:30-11:30 | ⚡ 实现 `src/tools/stack_parser.py`：`stack_parse(traceback_text)` → 正则解析 Python Traceback 格式。匹配 `File "(.+)", line (\d+), in (\w+)` 提取帧列表；匹配 `(\w+Error): (.+)` 提取异常类型和消息。支持链式异常 `During handling of the above exception`。输出 `{exception_type, exception_message, frames: [{file, line, function}], is_chained}` | 3 层嵌套 traceback → 3 帧正确；SyntaxError → 额外提取 text 和 offset |
| 11:30-12:00 | 🔧 ast_parse + stack_parse 单测 | `tests/test_ast_parser.py` + `tests/test_stack_parser.py` 共 4 tests green |
| 14:00-15:00 | ⚡ 实现 `src/tools/git_tools.py`：`git_blame(file, line)` → `subprocess.run(["git","blame","-L",f"{line},{line}","--",file])` → 解析输出。`git_diff(commit_a, commit_b, path=None)` → `git diff commit_a..commit_b -- path`。无 git 仓库时降级返回 "not a git repository" | blame 返回正确 author + commit；diff 返回正确行变更 |
| 15:00-16:00 | ⚡ 实现 `src/tools/find_test.py`：`find_test_for_function(func_name, file_path)` → 启发式：① 同目录下是否有 `tests/` ② `search(f"def test_*{func_name}*")` 在测试文件中 ③ `search(f"import.*{module_name}")` 在测试文件中。返回 `[{test_file, test_function, confidence}]` | `calculator.py:add()` → 返回 `tests/test_calculator.py::test_add` |
| 16:00-17:00 | ⚡ 将 5 个新 Tool（ast_parse/stack_parse/git_blame/git_diff/find_test）注册到 `src/tools/` 的 `build_repair_tools()` 函数，遵循 M1 的 dataclass + auto_schema 模式 | 所有新 Tool 可通过 `build_repair_tools(ctx)` 获取 |
| 17:00-17:30 | 🔧 git_tools + find_test 单测 | `tests/test_git_tools.py` + `tests/test_find_test.py` 共 3 tests green |

**Day 22 验收：** AST 解析正确提取函数结构且不含注释。堆栈解析支持链式异常。Git 工具在有 git 历史的目录中返回正确结果。

---

### Day 23（周三）：4 个 Agent 定义 + System Prompts

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 创建 `src/prompts/` 目录。写 `localizer.txt` System Prompt：`"你是代码定位专家。根据异常堆栈和 AST 解析结果精确确定位到函数/方法。工具：ast_parse/stack_parse/read_file/search/git_blame。输出 JSON 格式 SuspectList：[{file_path, start_line, end_line, function_name, reason, confidence}]。不要修改代码，不要生成补丁。"` | Prompt 清晰约束角色边界 |
| 10:30-11:30 | ⚡ 写 `retriever.txt`：`"你是代码搜索专家。根据 SuspectList 并行搜索相关代码、调用方、测试文件、历史类似修复。工具：search/read_file/git_blame/git_diff/find_test。输出 JSON 格式 RetrievedContext。不要判断对错，不要生成补丁。"`。写 `patcher.txt`：`"你是补丁生成者。严格基于给定的 SuspectList 和 RetrievedContext 生成 unified diff。工具：read_file/write_file/patch_file。只改最小必要行数。不要自己重新定位——定位已由 Localizer 完成。不要运行测试——测试由 Verifier 完成。"` | 三个 Prompt 角色边界清晰 |
| 11:30-12:00 | 🔧 Prompt 模板单测：验证每个 prompt 包含必要的角色声明、工具列表、输出格式约束 | `tests/test_prompts.py` 3 tests green |
| 14:00-15:30 | ⚡ 实现 `src/agents/localizer.py`：`create_localizer(client, workspace)` 工厂函数。创建 Agent 实例：tools=[ast_parse, stack_parse, read_file, search, git_blame]，prefix=localizer.txt，max_steps=4，approval="auto"。同理 `create_retriever`：tools=[search, read_file, git_blame, git_diff, find_test]，max_steps=4。`create_patcher`：tools=[read_file, write_file, patch_file]，max_steps=3，approval="ask"。所有 Agent 的 `execute_tool` 经 ToolGateway 代理 | 3 个工厂函数各返回正确配置的 Agent |
| 15:30-16:30 | ⚡ 用 FakeClient 分别测试 3 个 Agent：Localizer → 输入堆栈 → 预期调用 ast_parse + stack_parse；Retriever → 输入 SuspectList → 预期调用 search + find_test；Patcher → 输入位置+上下文 → 预期调用 patch_file | 3 个 Agent 各自独立工作 |
| 16:30-17:30 | 🔧 Agent 工厂单测 + Agent 行为单测（FakeClient） | `tests/test_agents.py` 4 tests green |

**Day 23 验收：** 3 个 Agent 各自配置正确——工具集合不同、max_steps 不同、System Prompt 不同。FakeClient 测试验证每个 Agent 按预期调用工具。

---

### Day 24（周四）：Orchestrator + Skill 系统 + CLI repair

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 实现 `src/orchestrator.py`：`Orchestrator` 类。`__init__(localizer, retriever, patcher, blackboard)`。核心方法 `repair(issue: str, repo: str) -> RepairState`：① `_parse_issue(issue)` 正则提取语言/异常类型/文件名 → 生成 RepairPlan ② `_run_localizer(state)` + `_run_retriever(state)` 串行（先用 FakeClient 验证，后续 M6 改并行）③ `_merge_to_blackboard(state)` ④ `_run_patcher(state)`。返回 state | 一条完整流水线走通 |
| 10:30-11:30 | ⚡ 实现 `_parse_issue(issue)`：正则 `(\w+Error)[: ](.+)` 提取异常类型和消息；正则 `File "(.+)", line (\d+)` 提取文件行号。`_match_skill(issue)` → 遍历 Skill 字典匹配 trigger_pattern → 注入 suggested_tools 到 RepairPlan | `"TypeError at calculator.py:42"` → plan.issue_type="type_error", plan.suspect_files=["calculator.py"] |
| 11:30-12:00 | 🔧 Orchestrator 单测（FakeClient 预设序列） | `tests/test_orchestrator.py` 1 test green |
| 14:00-15:00 | ⚡ 实现 Skill 系统（简化版）：`src/skills.py` 中 `SKILL_REGISTRY: dict` 硬编码 4 个 Skill。每个 Skill：`name, trigger_pattern (regex), suggested_tools (list), example_patch (str)`。`match_skill(issue_text)` 遍历并返回第一个匹配。用字典而非 YAML（M8 再升级为 YAML） | `"ImportError: No module named 'requests'"` → 匹配 `python_import_error` Skill |
| 15:00-16:00 | ⚡ 完善 Orchestrator 中 Agent 调用：`_run_localizer(state)` → `self.localizer.ask(f"定位以下错误：\n{state.issue_input}\n\n修复计划：{state.repair_plan}")` → 解析返回 JSON → 填入 `state.suspect_locations`。同理 `_run_retriever` 和 `_run_patcher`。每个 Agent 的 ask 结果用 `json.loads` 解析，解析失败则记录 parse_error 到 state | 各 Agent 的 JSON 输出被正确解析填入 RepairState |
| 16:00-17:00 | ⚡ 实现 `src/cli.py`：`repair` 子命令——`python -m src.cli repair --issue "..." --repo ./demo`。`--verbose` 模式打印 `[Orchestrator]` `[Localizer]` `[Retriever]` `[Patcher]` 分阶段日志和时间。`--dry-run` 模式所有 Agent 以 dry_run=True 运行 | CLI repair 命令可跑通完整流水线 |
| 17:00-17:30 | 🔧 CLI repair 集成测试（FakeClient） | `tests/test_cli_repair.py` 1 test green |

**Day 24 验收：** 输入一段 Python 错误堆栈 → Orchestrator 调度 Localizer→Retriever→Patcher 顺次执行 → 输出补丁 diff。整个流程在 FakeClient 模式下跑通。

---

### Day 25（周五）：M5 真实 API 联调 + 收尾

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 真实 API 测试 Localizer：给一段真实的 TypeError 堆栈，验证 Agent 是否正确调用 ast_parse → stack_parse → read_file → 输出结构化 SuspectList JSON。记录 JSON 解析成功率 | JSON 解析率 > 80% 即可接受 |
| 10:30-11:30 | ⚡ 真实 API 测试 Patcher：给 SuspectList + RetrievedContext，验证 Agent 生成正确的 unified diff。观察是否有越权调用（比如 Patcher 尝试调 ast_parse） | 越权调用被 ToolGateway 拦截 |
| 11:30-12:30 | ⚡ 真实 API 测试完整流水线：`python -m src.cli repair --issue "TypeError: ... at calc.py:42" --repo ./demo/calculator`。记录总耗时和各 Agent 耗时。观察 Blackboard 是否有冲突 | 完整流水线 60-90s 内完成 |
| 14:00-15:00 | ⚡ 根据真实 API 结果调整各 Agent 的 System Prompt——重点：输出格式的精确性、不要输出多余的解释文本、JSON 必须合法 | JSON 解析率提升 |
| 15:00-16:00 | ⚡ Prompt 调优 A/B 策略：为每个 Agent 准备 2 个 prompt 变体 → 在同一 Case 上各跑 3 次 → 选 JSON 解析率更高者。记录 A/B 结果到 `docs/prompt-ab-results.md` | 至少 1 个 Agent 的 prompt 有明显优化 |
| 16:00-17:00 | ⚡ M5 复盘 + git tag m5-done。统计代码量。补充遗漏单测 | M5 正式完成 |
| 17:00-17:30 | 为 M6 Docker 准备：确认 Docker Desktop 已安装、`docker` CLI 可用、`pip install docker` | Docker 环境就绪 |

**Day 25 验收（M5 里程碑）：**
- [ ] RepairState 数据模型完整
- [ ] Blackboard 运行中正确记录 Agent 产出
- [ ] ToolGateway 拦截越权调用
- [ ] AST/堆栈/Git/find_test 4 个 Tool 可用
- [ ] 3 个 Agent 各自 System Prompt 约束角色边界
- [ ] Orchestrator 正确编排流水线
- [ ] CLI `repair` 命令可用
- [ ] 真实 API 完整流水线跑通至少 1 个 Case
- [ ] `pytest tests/ -v` 全绿（80+ tests）
- [ ] 代码量约 2500 行（Layer 1 1900 + M5 600）

---

### M5 周末缓冲

- 周六：为 Localizer/Retriever/Patcher 各补 1 个边界 Case 测试
- 周日：休息

---

## M6：Docker 沙箱 + 验证闭环 + 自愈循环（Week 11-12）

**目标：补丁在隔离容器内验证，宿主机零副作用。打通自愈闭环。**

---

### Day 26（周一）：Dockerfile + SandboxManager

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:00 | ⚡ 创建 `sandbox/` 目录。写 `Dockerfile.python`：`FROM python:3.11-slim` → `RUN apt-get update && apt-get install -y git ripgrep` → `RUN pip install pytest pytest-cov pytest-json-report ruff` → `COPY entrypoint.sh /entrypoint.sh` → `RUN chmod +x /entrypoint.sh` → `ENTRYPOINT ["/entrypoint.sh"]`。`docker build -t repair-agent/python-repair .` | 镜像构建成功，约 400MB |
| 10:00-11:00 | ⚡ 写 `sandbox/entrypoint.sh`：`#!/bin/bash`，接受子命令：`build <cmd>` → `cd /code && exec $@`；`test <cmd>` → `cd /code && exec $@`；`apply-patch <file> <diff_file>` → `cp $file $file.bak.$(date +%s) && patch -p0 < $diff_file`；`revert-patch <file>` → `cp $(ls -t $file.bak.* | head -1) $file` | 容器内脚本可用 |
| 11:00-12:00 | ⚡ 实现 `src/harness/sandbox_manager.py`：`SandboxManager` 类（封装 `docker-py`）。`async create(profile, repo_path)` → `docker.containers.run(image, "tail -f /dev/null", volumes={repo: {"bind":"/code","mode":"ro"}}, network_mode="none", mem_limit="4g", cpu_quota=200000, detach=True)` → 返回 `Sandbox(id, profile)`。`async execute(sandbox, cmd, timeout=600)` → `container.exec_run(cmd)` → `ExecResult(exit_code, stdout, stderr)`。`async destroy(sandbox)` → `container.kill()` + `container.remove()` | 宿主机 Python 代码创建容器、执行 `pip install -e /code`、拿到 exit_code、销毁容器 |
| 12:00-12:30 | 🔧 SandboxManager 单测（Mock docker SDK） | `tests/test_sandbox_manager.py` 2 tests green |
| 14:00-15:30 | ⚡ 实现 `src/harness/patch_applier.py`：`PatchApplier` 类。常量：`MAX_PATCHES=5, MAX_LINES=50, BACKUP_RETENTION=3`。`async apply(sandbox, patches)` → 逐个写 diff 到容器内 `/tmp/patch_{i}.diff` → 调 `entrypoint.sh apply-patch {file} /tmp/patch_{i}.diff` → 检查 exit_code → 失败则 `revert_all`。`async revert_all(sandbox, applied)` → 倒序遍历已应用的 patch → `entrypoint.sh revert-patch {file}` | 3 个补丁连续应用，第 2 个失败 → 全部回滚 |
| 15:30-16:30 | ⚡ 实现 `src/harness/python_runner.py`：`PythonTestRunner` 类。`async run(sandbox, test_path="")` → ① `sandbox.execute("entrypoint.sh build pip install -e /code")` → 失败返回 `TestResult(build_failed=True, build_log=stderr)` ② `sandbox.execute(f"entrypoint.sh test pytest /code/{test_path} --json-report -v")` → 解析 `.report.json` → `TestResult(total, passed, failed, error, failure_logs)` | 含 3 测试（2 通过 1 失败）→ TestResult(total=3, passed=2, failed=1) |
| 16:30-17:30 | 🔧 PatchApplier + TestRunner 单测（Mock Sandbox） | `tests/test_harness.py` 3 tests green |

**Day 26 验收：** Docker 镜像构建成功。宿主机 Python 可控制容器内执行命令。补丁应用和回滚原子化。pytest 结果正确解析。

---

### Day 27（周二）：sandbox Tool + Verifier Agent

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 实现 `src/tools/sandbox_tools.py`：`sandbox_build(repo_path)` — 创建独立容器 → `pip install -e /code` → 返回 `{exit_code, stdout, stderr}` → 销毁容器。`sandbox_test(repo_path, test_path)` — 创建独立容器 → 构建 + 运行测试 → 返回结构化 `{all_passed, total, passed, failed, error, failure_logs}` → 销毁容器。`risky=False`（容器内执行，宿主机安全），ToolGateway 权限：仅 verifier | Localizer 调用 sandbox_test → permission_denied |
| 10:30-11:30 | ⚡ 实现 `src/agents/verifier.py`：`create_verifier(client, workspace, sandbox_manager)` 工厂函数。tools=[sandbox_build, sandbox_test]，System Prompt verifier.txt：`"你是验证执行者。在 Docker 容器内应用补丁、构建、运行测试。工具：sandbox_build/sandbox_test。只报告结果，不修改代码，不重新定位。输出 JSON 格式 VerificationResult。"`，max_steps=2，approval="auto" | Verifier Agent 正确配置 |
| 11:30-12:00 | 🔧 sandbox_tools + verifier 单测（Mock SandboxManager） | `tests/test_sandbox_tools.py` + `tests/test_verifier.py` 共 3 tests green |
| 14:00-15:30 | ⚡ 扩展 Orchestrator 接入 Verifier：`repair()` 中 Patcher 完成后 → `_run_verifier(state)` → 调用 Verifier Agent → 解析 VerificationResult → 写入 state。`_evaluate_result(state)` → `all_passed` → `state.status="fixed"` → 结束。`not all_passed` + `retry_count < max_retries` → 进入自愈 | 流水线延伸为 Localizer→Retriever→Patcher→Verifier |
| 15:30-17:00 | ⚡ 实现自愈反馈循环：`_build_feedback(result: VerificationResult)` → 提取 `failure_logs` 中每条失败测试的名称和错误消息 → 格式化为：`"补丁验证失败。以下测试仍失败：\n- test_add: AssertionError: assert 3 == 5\n- test_sub: TypeError: ...\n请修改补丁解决这些问题。"`。`_run_patcher_with_feedback(state)` → 在 Patcher 的 user_message 前追加 `[上一轮验证反馈]\n{feedback}\n\n[原始任务]\n{original_issue}`。最多 3 轮，每轮 `retry_count += 1` | 模拟需要 2 次修复的 Case → Patcher 被调用 2 次 |
| 17:00-17:30 | 🔧 自愈循环单测（FakeClient 预设 Patcher 两次不同输出） | `tests/test_self_healing.py` 2 tests green |

**Day 27 验收：** Verifier Agent 可正确调 sandbox_test。Orchestrator 中自愈循环正确运转——失败反馈注入 Patcher，重写后再次验证。

---

### Day 28（周三）：真实 Docker 集成 + 端到端闭环测试

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 准备测试用微型 Python 项目：`demo/calculator/` — `calculator.py`（含 add/subtract 函数 + TypeError bug）、`test_calculator.py`（3 个测试，1 个预期失败）、`pyproject.toml`。手动确认 `cd demo/calculator && pip install -e . && pytest` → 1 个测试失败 | 测试项目就绪 |
| 10:30-12:00 | ⚡ 真实 API + 真实 Docker 完整闭环测试：`python -m src.cli repair --issue "TypeError: unsupported operand type(s) for +: 'int' and 'str' at calculator.py:15" --repo ./demo/calculator --verbose`。观察：① Localizer 正确定位 ② Patcher 生成补丁 ③ Verifier 在容器内跑测试 ④ 结果返回 | 首次完整闭环跑通 |
| 12:00-12:30 | 🔧 修复集成中发现的问题（容器路径、权限、超时） | Bug 修复 |
| 14:00-15:30 | ⚡ 测试自愈循环：修改 `calculator.py` 使 bug 更复杂（需要同时改类型转换和空值判断），构造需要 2 次尝试的场景。运行 repair → 观察第 1 次失败 → 反馈 → 第 2 次通过 | 自愈循环真实生效 |
| 15:30-16:30 | ⚡ 测试边界情况：① 构建失败（setup.py 语法错误）→ Verifier 返回 build_failed ② 全部测试通过（bug 已修复）→ Verifier 返回 all_passed ③ 超时（设置 10s timeout，跑慢测试）→ 容器被 kill | 3 种边界情况正确处理 |
| 16:30-17:30 | 🔧 边界情况测试用例化 | `tests/test_e2e_repair.py` 3 tests green |

**Day 28 验收：** 真实 Docker 容器内跑通完整修复闭环。自愈循环成功触发重试。边界情况正确处理。

---

### Day 29（周四）：多 Case 验证 + 性能优化 + Prompt 精调

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 准备第 2 个 Demo 项目：`demo/importer/` — 含 ImportError（缺少 `__init__.py` 或 `import` 路径错误）。`demo/logic_bug/` — 含逻辑错误（off-by-one）。各含 2-3 个测试 | 3 个不同错误类型的 Demo 项目就绪 |
| 10:30-12:00 | ⚡ 对 3 个 Demo 各跑一次 repair，记录：① Fix/NotFix ② 耗时 ③ retry 次数 ④ 各 Agent 的 JSON 解析成功率。汇总到表格 | 3 Case 中至少 1 个修复成功 |
| 12:00-12:30 | 🔧 根据 3 Case 结果调整各 Agent Prompt（重点：Localizer 的 JSON 输出格式、Patcher 的 diff 格式） | Prompt 优化 |
| 14:00-15:00 | ⚡ 性能优化：① Orchestrator 中 Localizer 和 Retriever 改为 `asyncio.gather` 并行执行（预期节省 20-30% 时间）② 容器镜像预 pull，避免首次运行时下载延迟 ③ SandboxManager 连接池复用 | 单 Case 修复时间下降 |
| 15:00-16:00 | ⚡ 超时与资源保护加固：① Orchestrator 层添加总超时 `asyncio.wait_for(repair(), timeout=180)` ② SandboxManager 层构建超时 600s、测试超时 900s ③ 容器退出后确保 `destroy()` 被调用（try/finally） | 资源不泄漏 |
| 16:00-17:00 | ⚡ 错误处理完善：Orchestrator 中每个 Agent 调用包裹 try/except，Agent 失败不导致整个流水线崩溃，而是记录 error 到 state 并尝试降级（如 Retriever 失败 → Patcher 只在 SuspectList 上工作） | 单点 Agent 失败不崩溃 |
| 17:00-17:30 | 🔧 并行 + 超时 + 降级单测 | `tests/test_orchestrator_robustness.py` 3 tests green |

**Day 29 验收：** 3 个不同错误类型的 Demo 至少 1 个修复成功。Localizer+Retriever 并行执行。超时和降级保护生效。

---

### Day 30（周五）：M6 收尾 + M1-M6 总验收

| 时间 | 任务 | 产出 |
|------|------|------|
| 09:00-10:30 | ⚡ 全量测试通过：`pytest tests/ -v --cov=agent_runtime --cov=src` 全部绿色。目标 90+ tests，覆盖率 > 70% | 全量测试通过 |
| 10:30-12:00 | ⚡ 代码整理：`ruff check` + `ruff format` 零 warning。补全所有公开函数 docstring。检查 `.gitignore`（`.agent/`、`__pycache__/`、`.env`） | 代码整洁 |
| 12:00-12:30 | 🔧 删除调试 print、清理注释掉的代码、统一命名风格 | 代码质量 |
| 14:00-15:00 | ⚡ 自愈闭环 Demo 录制准备：写 `demo/demo_repair.sh` 脚本——一个可独立运行的 shell 脚本，展示完整的 `repair --verbose` 过程。确保可复现 | Demo 脚本可运行 |
| 15:00-16:00 | ⚡ M6 复盘 + M1-M6 总结：① 统计最终代码行数 ② 统计测试数 ③ 统计覆盖的 bullet 点 ④ 记录已知问题和改进方向。git tag m6-done | M6 正式完成 |
| 16:00-17:00 | ⚡ Layer 2 完成：总览当前项目能力矩阵，为 M7 评测做准备 | Layer 2 完成 |

**Day 30 验收（M6 里程碑）：**
- [ ] Docker 沙箱完整运作，宿主机零测试工具链
- [ ] Verifier 正确执行容器内构建和测试
- [ ] 自愈循环最多 3 轮重试
- [ ] 完整闭环 → 定位+检索 → 补丁 → 容器验证 → 失败反馈 → 重写 → 通过
- [ ] Localizer+Retriever 并行执行
- [ ] 超时和降级保护生效
- [ ] `pytest tests/ -v --cov` 全绿，90+ tests
- [ ] 代码量约 3000 行（Layer 1 1900 + M5 600 + M6 500）

---

## 附录 A：M5-M6 新增文件清单

```
src/
├── state.py                # M5 Day21
├── blackboard.py           # M5 Day21
├── middleware.py           # M5 Day21
├── orchestrator.py         # M5 Day24
├── skills.py               # M5 Day24
├── cli.py                  # M5 Day24
├── agents/
│   ├── localizer.py        # M5 Day23
│   ├── retriever.py        # M5 Day23
│   ├── patcher.py          # M5 Day23
│   └── verifier.py         # M6 Day27
├── prompts/
│   ├── localizer.txt       # M5 Day23
│   ├── retriever.txt       # M5 Day23
│   ├── patcher.txt         # M5 Day23
│   └── verifier.txt        # M6 Day27
├── tools/
│   ├── ast_parser.py       # M5 Day22
│   ├── stack_parser.py     # M5 Day22
│   ├── git_tools.py        # M5 Day22
│   ├── find_test.py        # M5 Day22
│   └── sandbox_tools.py    # M6 Day27
└── harness/
    ├── sandbox_manager.py  # M6 Day26
    ├── patch_applier.py    # M6 Day26
    └── python_runner.py    # M6 Day26

sandbox/
├── Dockerfile.python       # M6 Day26
└── entrypoint.sh           # M6 Day26

demo/
├── calculator/             # M6 Day28
├── importer/               # M6 Day29
└── logic_bug/              # M6 Day29

tests/
├── test_state.py           # M5 Day21
├── test_blackboard.py      # M5 Day21
├── test_middleware.py      # M5 Day21
├── test_ast_parser.py      # M5 Day22
├── test_stack_parser.py    # M5 Day22
├── test_git_tools.py       # M5 Day22
├── test_find_test.py       # M5 Day22
├── test_prompts.py         # M5 Day23
├── test_agents.py          # M5 Day23
├── test_orchestrator.py    # M5 Day24
├── test_cli_repair.py      # M5 Day24
├── test_sandbox_manager.py # M6 Day26
├── test_harness.py         # M6 Day26
├── test_sandbox_tools.py   # M6 Day27
├── test_verifier.py        # M6 Day27
├── test_self_healing.py    # M6 Day27
├── test_e2e_repair.py      # M6 Day28
└── test_orchestrator_robustness.py # M6 Day29
```

## 附录 B：M1-M6 累计指标

| 里程碑 | 代码量 | 测试数 | 核心能力 |
|:--:|:--:|:--:|------|
| M1 | 500 | 20 | 控制循环 + 3 工具 + Config + Workspace |
| M2 | 1000 | 40 | 6 工具 + 7 闸口 + Token 预算 + Dry-Run |
| M3 | 1500 | 55 | 三层记忆 + Checkpoint + 持久化 + 安全 |
| M4 | 1900 | 70 | 语义记忆 + 配额 + 熔断 + Replay |
| M5 | 2500 | 85 | 4 Agent + Blackboard + ToolGateway + 新 Tool + Skill |
| M6 | 3000 | 95 | Docker Harness + Verifier + 自愈闭环 + 3 Demo |
