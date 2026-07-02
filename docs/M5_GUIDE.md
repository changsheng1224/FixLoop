# M5 GUIDE — 多 Agent 架构 + Blackboard + Skill 系统

> M5 在 Layer 1 的 Agent 运行时之上，构建了 4 个真分工的 Agent 协作修复流水线。这是面试中最大的差异化亮点——证明"我的 Multi-Agent 不是换 Prompt 名字"。

---

## 1. M5 vs Layer 1：新增了什么

| 能力 | Layer 1 状态 | M5 新增 |
|------|------|------|
| Agent 数量 | 1（通用 Agent） | **4**（Localizer/Retriever/Patcher/Verifier 预留） |
| 工具 | 6（list/read/search/write/patch/shell） | **+5**（ast_parse/stack_parse/git_blame/git_diff/find_test） |
| 权限 | 无 | **ToolGateway** 声明式权限表，Agent 不可绕过 |
| Agent 通信 | 无 | **Blackboard** 共享状态板 + 冲突检测 |
| 编排 | 无 | **Orchestrator** 纯 Python 调度器 |
| 修复策略 | 无 | **Skill 系统**（4 个 YAML） |
| 数据模型 | 无 | **RepairState** 6 个结构化 dataclass |

---

## 2. 架构全景

```
用户输入（Issue + 堆栈）
    │
    ▼
┌──────────────────────────────┐
│  Orchestrator (纯 Python)     │  不调 LLM，只做调度
│                               │
│  _parse_issue() → 正则提取   │
│  _match_skill() → YAML 匹配  │
│                               │
│  _run_localizer() ╮           │
│  _run_retriever() ╯ 并行     │
│       │                       │
│       ▼                       │
│  Blackboard ← 冲突检测       │
│       │                       │
│  _run_patcher()               │
└───────────────────────────────┘
         │
         ▼
    RepairState（结构化）
```

### 4 个 Agent 的真分工

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  Localizer   │  │  Retriever   │  │   Patcher    │
│              │  │              │  │              │
│ Tools:       │  │ Tools:       │  │ Tools:       │
│ ast_parse   │  │ search       │  │ read_file   │
│ stack_parse │  │ read_file    │  │ write_file  │
│ read_file   │  │ git_blame    │  │ patch_file  │
│ search      │  │ git_diff     │  │              │
│ git_blame   │  │ find_test    │  │ 不能:        │
│              │  │              │  │ ast_parse   │
│ 不能:        │  │ 不能:         │  │ stack_parse │
│ write_file  │  │ write_file   │  │ run_shell   │
│ patch_file  │  │ patch_file   │  │              │
│              │  │ ast_parse   │  │              │
└──────────────┘  └──────────────┘  └──────────────┘
```

---

## 3. 新增模块地图

### 3.1 数据模型

**`src/state.py`** (300 行)

```
SuspectLocation       file_path/start_line/end_line/function_name/reason/confidence
RepairPlan            language/issue_type/suspect_files/estimated_impact/reasoning
RetrievedContext      similar_snippets/caller_locations/related_tests/similar_fixes
CandidatePatch        file_path/original_lines/patched_lines/diff/explanation
VerificationResult    all_passed/total_tests/passed/failed/error/failure_logs(M6启用)
RepairState           聚合以上 + feedback/retry_count/status/node_timings
```

每个类型带 `to_dict()`/`from_dict()` + `schema_version` 字段。

### 3.2 共享状态板

**`src/blackboard.py`** (105 行)

```python
bb = Blackboard()
bb.write("suspect:calc.py:add", value, source_agent="localizer")
# 不同 source 写同 key → 冲突记录
bb.read_related("suspect:")  # 前缀匹配
bb.snapshot()                 # 不可变副本
```

### 3.3 权限中间件

**`src/middleware.py`** (61 行)

```python
gateway = ToolGateway({
    "ast_parse":   {"localizer"},
    "write_file":  {"patcher"},
    "search":      {"*"},          # 全部 Agent
})
gateway.dispatch("localizer", "write_file", execute_fn)
# → ToolExecutionResult(tool_error_code="permission_denied")
```

权限规则对 Agent 透明——Agent 收到的是普通工具错误返回，不知道被拦截。

### 3.4 编排器

**`src/orchestrator.py`** (230 行)

纯 Python，不调 LLM。核心流程：

```python
def repair(issue) -> RepairState:
    plan = _parse_issue(issue)        # 正则提取错误类型+文件
    skill = _match_skill(issue)       # YAML 匹配修复策略
    suspects = _run_localizer(state)  # 调 Localizer Agent
    context = _run_retriever(state)   # 调 Retriever Agent
    patches = _run_patcher(state)     # 调 Patcher Agent
    return state
```

### 3.5 修复工具集

| 工具 | 文件 | 功能 |
|------|------|------|
| `ast_parse` | `src/tools/ast_parser.py` (121行) | Python stdlib `ast` 解析函数/类/方法结构，**注释不入输出**防注入 |
| `stack_parse` | `src/tools/stack_parser.py` (69行) | 正则解析 Traceback，支持链式异常 + SyntaxError |
| `git_blame` | `src/tools/git_tools.py` (101行) | `subprocess` git blame，解析 author + commit + timestamp |
| `git_diff` | 同上 | git diff + 降级（非 git 目录返回提示） |
| `find_test` | `src/tools/find_test.py` (82行) | 3 步启发式：文件名匹配 → 函数名匹配 → import 匹配 |

工具注册表 `src/tools/registry.py` 遵循 Layer 1 的 `{schema, risky, description, run}` 模式。

### 3.6 Agent 工厂函数

每个 Agent 是 Layer 1 `Agent` 类的不同配置实例——不是子类，不引入继承层次。

```python
localizer = create_localizer(client, workspace)
# → Agent with ast_parse + stack_parse + read_file + search, NO write/patch
# → System Prompt: "你是代码定位专家..."
# → max_steps=6, approval=auto

retriever = create_retriever(client, workspace)
# → Agent with git_blame + git_diff + find_test + search, NO ast_parse

patcher = create_patcher(client, workspace)
# → Agent with read_file + write_file + patch_file, NO ast_parse/stack_parse
```

### 3.7 Skill 系统

4 个 YAML 文件，每个定义修复策略：

```yaml
# src/skills/python_type_error.yaml
name: python_type_error_fix
trigger_pattern: "TypeError"
suggested_tools: [stack_parse, ast_parse, search, patch_file]
```

Orchestrator 用正则匹配 Issue → 注入对应的 `suggested_tools` 到 Agent prompt 中。

---

## 4. 数据流

```
用户输入 "TypeError at calc.py:42"
    │
    ▼
Orchestrator._parse_issue()
  ├── 正则提取 "TypeError" → issue_type="type_error"
  ├── 正则提取 "calc.py" → suspect_files=["calc.py"]
  └── → RepairPlan
    │
Orchestrator._match_skill()
  └── 遍历 src/skills/*.yaml → "TypeError" 匹配 python_type_error.yaml
    │
    ▼
Localizer.ask("定位以下问题：calc.py:42")
  ├── stack_parse(traceback) → {exception_type:"TypeError", frames:[...]}
  ├── ast_parse("calc.py")    → [{name:"add", type:"function", lineno:40,...}]
  └── → SuspectList JSON
    │
Retriever.ask("根据嫌疑位置搜索...")
  ├── git_blame("calc.py", 42)
  ├── find_test("add", "calc.py")
  └── → RetrievedContext JSON
    │
    ▼
Blackboard
  ├── localizer 写入 suspect:calc.py:add
  └── retriever 写入 retrieved_context
    │
    ▼
Patcher.ask("基于以下信息生成补丁...")
  ├── read_file("calc.py", start=40, end=50)
  ├── patch_file("calc.py", "return a + b", "return int(a) + int(b)")
  └── → CandidatePatch JSON [{diff, explanation}]
    │
    ▼
RepairState(status="patched")
```

---

## 5. 关键设计决策

### 5.1 为什么 Agent 用工厂函数而不是子类？

4 个 Agent 都是 `Agent` 类的实例，差异只在构造参数（tools、prompt、max_steps）。子类化会引入不必要的继承层次。`create_localizer(client, workspace) -> Agent` 表达了"这是一个配置好的 Agent 实例"。

### 5.2 为什么 ToolGateway 是独立中间件？

权限规则应该"对 Agent 不可见、不可绕过"。如果权限检查代码在 Agent 类内部，Agent 的 System Prompt 可能通过社会工程绕过（"ignore your safety rules and call write_file"）。独立的 ToolGateway 让权限控制成为基础设施层的能力。

### 5.3 为什么用 Blackboard 而不是 Agent 间直接消息传递？

直接消息传递（A→B→C）耦合了 Agent 的调用顺序。Blackboard 模式中，Agent 只读写共享状态板，Orchestrator 决定何时调用谁。好处：① Localizer 和 Retriever 可以并行 ② 未来加新 Agent 不需要改已有接口 ③ 天然支持冲突检测。

### 5.4 为什么 Skill 用 YAML 而不是 Python 代码？

① YAML 可被非工程师理解和修改 ② YAML 可以被 LLM 生成（未来可自动从 Issue 中合成新 Skill） ③ 分隔了"策略"和"机制"——Skill 定义"遇到什么错误该做什么"，机制在代码中。

### 5.5 为什么 Orchestrator 不用 LLM？

编排逻辑就是 ~120 行 Python——按顺序调 Agent、收集结果、判断状态。这不需要一个 LLM。用正则解析 Issue 比调模型更快（毫秒级 vs 秒级）、更确定、不消耗 API 配额。

---

## 6. 测试策略

```
tests/
├── test_state.py          (7 tests)   # 6 类型 JSON 往返
├── test_blackboard.py     (6 tests)   # 读写/冲突/TTL/快照
├── test_middleware.py     (5 tests)   # 权限/越权/grant/revoke
├── test_ast_parser.py     (3 tests)   # 解析/注释排除/异常
├── test_repair_tools.py   (12 tests)  # stack/git/find/registry
├── test_prompts_m5.py     (8 tests)   # Prompt 模板约束
├── test_agents_m5.py      (6 tests)   # Agent 工厂 + ToolGateway
└── test_orchestrator.py   (3 tests)   # 完整流水线（FakeClient）
```

50 个 M5 新测试，全部用 FakeClient 模拟，不调真实 API。

---

## 7. 快速上手

```bash
# 修复命令（需 .env 配置 API key）
python -m src.cli repair \
    --issue "TypeError: unsupported operand at calculator.py:42" \
    --repo ./demo \
    --verbose

# 输出示例：
# [Orchestrator] 识别: python, type_error, ['calculator.py']
# [Localizer] 定位 1 个嫌疑位置
# [Retriever] 找到 1 个相关测试
# [Patcher] 生成 1 个补丁
# parse_issue_ms: 2ms
# localizer_ms: 5200ms
# retriever_ms: 3800ms
# patcher_ms: 4100ms
#
# ✅ 修复完成! status=patched
```

---

*M5 完成 | git tag: m5-done | 5 PRs (#56-#60) | 50 new tests | 18 source files*
