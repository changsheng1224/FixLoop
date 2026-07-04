# L1 / L2 边界重构计划

> **Phase 0 产出**（只读审计，2026-07-04）  
> **L1** = `agent_runtime/` — 通用 Agent 运行时，唯一核心入口 `Agent.ask()`  
> **L2** = `src/` — 多 Agent 修复 + Harness + Eval，**只能**依赖 L1  
> **非目标**：引入 LangChain、合并包、改 CLI 语义  
> **约束**：476 pytest 全绿、ruff 零 warning、每 Phase 独立 PR <400 行

参考：[ARCHITECTURE.md](../../ARCHITECTURE.md)、[design-decisions.md](../design-decisions.md)、[FINAL_STATS.md](../FINAL_STATS.md)

---

## 1. 现状摘要

| 指标 | 数值 |
|------|------|
| L1 生产代码 | ~4,578 行 / 31 文件 |
| L2 生产代码 | ~5,086 行 / 37 文件 |
| L2 文件直接 import L1 | **9 / 37**（24%） |
| L1 import L2 | **0**（方向正确） |
| 触及的 L1 模块 | **8 / ~31**（26%） |

**架构偏离点（无 import，但违反「唯一入口」原则）：**

- `src/orchestrator.py::_complete_with_system_prompt` 直接调用 `agent.model_client.complete()`，Patcher 绕过 `Agent.ask()` / `AgentLoop`。
- `src/middleware.py::ToolGateway.wrap_agent` 在运行时替换 `agent.execute_tool`（monkey-patch），权限不在 L1 闸口内。

---

## 2. L2 → L1 耦合矩阵

扫描范围：`src/**/*.py`（AST 静态 import + 已知 lazy import）。  
符号：`●` = 该文件 import 此 L1 模块；`○` = 间接使用（持有 L1 对象，无 import）。

### 2.1 按 L2 文件 × L1 模块

| L2 文件 | config | runtime | tools | tool_context | schema_utils | tool_executor | providers.clients | workspace |
|---------|:------:|:-------:|:-----:|:------------:|:------------:|:-------------:|:-----------------:|:---------:|
| `agents/localizer.py` | ● | ● | ● | ● | | | | |
| `agents/patcher.py` | ● | ● | ● | ● | | | | |
| `agents/retriever.py` | ● | ● | ● | ● | | | | |
| `agents/verifier.py` | ● | ● | | ● | | | | |
| `repair_factory.py` | | | | | | | ● | ● |
| `tools/registry.py` | | | | | ● | | | |
| `tools/sandbox_tools.py` | | | | ●† | | | | |
| `middleware.py` | | | | | | ●† | | |
| `eval/baseline.py` | ● | ● | ● | ● | | | | ● |
| `orchestrator.py` | ○ | ○ | | | | | ○ | |
| `cli.py` | | | | | | | | |
| `harness/*` | | | | | | | | |
| 其余 L2 | | | | | | | | |

† lazy import（函数体内）

### 2.2 按 L1 模块 × 消费方（fan-in）

| L1 模块 | L2 消费文件数 | 导入符号 | 层级角色 |
|---------|:------------:|----------|----------|
| `agent_runtime.runtime.Agent` | 5 | `Agent` | **核心 façade**（应保留） |
| `agent_runtime.config.AgentConfig` | 5 | `AgentConfig` | 配置（应保留） |
| `agent_runtime.tools.build_tool_registry` | 4 | `build_tool_registry` | 工具装配（待收敛） |
| `agent_runtime.tool_context.ToolContext` | 6 | `ToolContext` | 路径锚定（应保留） |
| `agent_runtime.workspace.WorkspaceContext` | 2 | `WorkspaceContext` | 工作区（待 bootstrap 收敛） |
| `agent_runtime.providers.clients.*` | 1 | `AnthropicCompatibleModelClient` | 客户端（待 bootstrap 收敛） |
| `agent_runtime.schema_utils.auto_schema` | 1 | `auto_schema` | 工具 schema（应保留） |
| `agent_runtime.tool_executor.ToolExecutionResult` | 1 | `ToolExecutionResult` | 执行结果类型（待 gateway 收敛） |

### 2.3 按 L2 子系统

| 子系统 | 触及 L1 | 说明 |
|--------|---------|------|
| `src/agents/` | runtime, config, tools, tool_context | 四个 `create_*` 工厂高度同构 |
| `src/repair_factory.py` | clients, workspace | 与 `agent_runtime/cli.py` 重复 dotenv + client |
| `src/tools/` | schema_utils, tool_context | L2 扩展工具注册表 |
| `src/middleware.py` | tool_executor | 权限网关 monkey-patch |
| `src/eval/baseline.py` | 6 个 L1 模块 | 与 agents + repair_factory 三重装配 |
| `src/orchestrator.py` | 无 import，○  duck-type | Patcher 直调 `model_client.complete` |
| `src/harness/` | 无 | 纯 Docker/pytest，边界清晰 ✓ |

---

## 3. Top 5 冗余点与建议归属

| # | 冗余描述 | 现状位置 | 建议归属 | 理由 |
|---|----------|----------|----------|------|
| **R1** | **dotenv + ModelClient 装配** | `agent_runtime/cli.py`（`_load_dotenv`, `_build_model_client`）与 `src/repair_factory.py`（`load_dotenv`, `create_model_client`）逻辑几乎相同 | **新边界 API**：`agent_runtime.bootstrap` | L1 已有 CLI 装配；L2 不应复制 env/client 细节。单一来源便于 FakeClient 测试注入。 |
| **R2** | **双工具注册表 + 手工 merge** | L1 `build_tool_registry` + L2 `build_repair_tools` / `build_sandbox_tool_registry`；各 agent 内 `tools.update` / `pop` | **L2 内聚**：`src/tools/composite.py::build_repair_agent_tools(role)` | 修复域工具属 L2；合并逻辑不应散落在 4 个 agent 文件。L1 保持通用 6 工具不变。 |
| **R3** | **四个 `create_*` Agent 工厂** | `src/agents/{localizer,patcher,retriever,verifier}.py` | **L2 内聚**：`src/agents/factory.py` + 薄 wrapper | 共享 `Agent(...)` 构造、gateway wrap、config 默认值；verifier 仅 tools 不同。 |
| **R4** | **Orchestrator 绕过 Agent loop** | `orchestrator._complete_with_system_prompt` → `model_client.complete` | **新边界 API**：`Agent.complete_once(user_message)` 或 `ModelClient.complete_with_system(...)` | 保留「单次 completion、无 tool loop」语义，但经 L1 入口，便于 token/trace/测试统一。 |
| **R5** | **ToolGateway monkey-patch** | `middleware.wrap_agent` 替换 `execute_tool` | **新边界 API（可选 Phase 4+）**：`Agent(tool_filter=...)` 或 L1 `ToolExecutor` 钩子 | 权限应在 L1 闸口内；L2 只声明 `REPAIR_PERMISSION_TABLE`。Phase 1–3 可暂保留 wrap，先收敛装配。 |

**未进 Top 5 但记录：**

- Eval `run_pytest` 被 Orchestrator 与 Eval runner 共用 → 已在 L2 内，边界合理。
- Baseline 重复 agent 装配 → 随 R2/R3 一并消除。

---

## 4. 目标边界 API（草图）

原则：**L2 只 import 下列公开面 + 必要的类型**；其余 L1 模块视为内部实现。

```python
# agent_runtime/__init__.py 或 agent_runtime/public.py（Phase 1 起逐步导出）

# --- 核心（已有，保留）---
from agent_runtime.config import AgentConfig
from agent_runtime.runtime import Agent
from agent_runtime.tool_context import ToolContext
from agent_runtime.workspace import WorkspaceContext

# --- Phase 1：bootstrap（消除 R1）---
from agent_runtime.bootstrap import (
    load_dotenv,           # 从 cli._load_dotenv 提取
    create_model_client,   # env + provider → ModelClient；支持 inject fake
)

# --- Phase 2：单次 completion（消除 R4 直调 model_client）---
# 方法挂在 Agent 上，或独立函数：
# Agent.complete_once(self, user_message: str) -> str
#   使用 agent 已有 system_prompt + config.max_new_tokens，不走 AgentLoop

# --- Phase 3：L2 工具装配（R2，纯 L2，无 L1 变更）---
# src/tools/composite.py
# def build_repair_agent_tools(ctx, role: Literal["localizer","retriever","patcher","verifier","baseline"]) -> dict: ...

# --- Phase 4+（可选）：权限钩子（R5）---
# Agent(..., tool_policy: Callable[[str, str], bool] | None = None)
# 或 ToolExecutor.register_pre_hook(...)
```

### 4.1 L2 允许 import 清单（目标态）

| 类别 | 模块 | 用途 |
|------|------|------|
| 必须 | `runtime`, `config`, `tool_context` | Agent 构造与 ask |
| 必须 | `schema_utils.auto_schema` | L2 工具 dataclass → schema |
| 收敛后 | `bootstrap` | env + client（替代直引 `providers.clients`） |
| 收敛后 | `workspace` | 仅 `repair_factory` / eval 装配 |
| 禁止（目标） | 直引 `providers.clients` from L2 业务代码 | 经 bootstrap |
| 禁止（目标） | 直调 `agent.model_client.complete` from orchestrator | 经 `Agent.complete_once` |
| 暂缓 | `tool_executor.ToolExecutionResult` in middleware | 至 Phase 4 gateway 下沉 |

### 4.2 依赖方向（不变）

```
src/  ──import──▶  agent_runtime/
                      ✗ 不得 import src/
tests/ ──import──▶  agent_runtime/ + src/   （测试不受限）
```

---

## 5. 迁移顺序（每 Phase 独立 PR，<400 行）

| Phase | 分支建议 | 内容 | 预估行数 | 测试范围 |
|-------|----------|------|:--------:|----------|
| **0** | — | 本文档 + 耦合矩阵（只读） | 0 代码 | — |
| **1** | `refactor/L1-bootstrap` | 新增 `agent_runtime/bootstrap.py`；`repair_factory` 改 import；删除 L2 `load_dotenv`/`create_model_client` 重复实现；CLI 改调 bootstrap | ~120 | `test_cli.py`, `test_cli_repair.py`, `test_repair_factory`（若有）, `test_baseline.py` |
| **2** | `refactor/L2-tool-composite` | 新增 `src/tools/composite.py`；4 agents + baseline 改调；删除分散 merge/pop | ~180 | `test_agents_m5.py`, `test_repair_tools.py`, `test_sandbox_tools.py`, `test_baseline.py` |
| **3** | `refactor/L1-complete-once` | `Agent.complete_once()`；orchestrator Patcher 改入口；补单测 | ~100 | `test_orchestrator.py`, `test_agent_loop.py`（新 case） |
| **4** | `refactor/L2-agent-factory` | `src/agents/factory.py` 合并 create_* | ~200 | `test_agents_m5.py`, e2e repair |
| **5** | `refactor/L1-tool-policy` |（可选）L1 tool 权限钩子；middleware 去 monkey-patch | ~250 | `test_middleware`, tool executor |
| **6** | `refactor/public-exports` | `agent_runtime` 公开面文档化 + ruff 边界 lint（可选） | ~80 | 全量 476 |

**Phase 1 具体 diff 预览（推荐先做）：**

1. 新建 `agent_runtime/bootstrap.py`：
   - `load_dotenv(cwd: Path | None = None) -> None`
   - `create_model_client(*, model=..., base_url=..., api_key=..., provider="anthropic_compat", inject=None) -> ModelClient`
2. `agent_runtime/cli.py`：`_load_dotenv` / client 构建改为调用 bootstrap（行为不变）。
3. `src/repair_factory.py`：删除本地 `load_dotenv` 与 `create_model_client` 实现，改为 `from agent_runtime.bootstrap import ...`。
4. **不**改 CLI 参数语义、不合并包。

---

## 6. 验收标准（全程）

- [ ] `pytest tests/ -v` → 476 passed
- [ ] `ruff check agent_runtime src tests` → 0 warning
- [ ] `rg "from src" agent_runtime/` → 0 matches
- [ ] 每 PR diff stat < 400 lines（不含本文档）
- [ ] ARCHITECTURE.md 在 Phase 6 后更新 L1 公开 API 小节（非 Phase 0–1 必须）

---

## 7. Phase 0 结论

1. **依赖方向正确**：L1 零反向依赖；L2 仅 9 文件、8 个 L1 模块直接接触。
2. **主要问题不是 import 数量，而是语义泄漏**：装配重复（R1）、工具 merge 重复（R2）、Orchestrator 绕过 `ask()`（R4）。
3. **推荐 Phase 1**：`agent_runtime.bootstrap` + 删除 `repair_factory` 重复 — 最小风险、立刻收窄 L2→`providers.clients` 接触面。
4. **CLI 语义与非目标**：全程保持 `python -m agent_runtime` 与 `python -m src.cli` 行为不变。

---

*文档版本：Phase 0 · commit base `v1.0.0` (`23b6c22`)*
