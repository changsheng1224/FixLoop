# 工具 schema 集稳定化 — 设计规格

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §5.1 — 工具 schema 集稳定化 [P1]
- **Layer:** L1 + L2
- **Primary modules:** `src/tools/composite.py`, `agent_runtime/prompt_prefix.py`, `agent_runtime/runtime.py`, `agent_runtime/context_manager.py`
- **Acceptance:** `pytest tests/test_repair_tool_schema_stable.py tests/test_prefix_stable.py tests/test_orchestrator.py -v`
- **Branch:** `V1.1-Bonus4-Prompt`

## 方案 A（已实现）

- `REPAIR_CANONICAL_TOOL_NAMES`：14 工具字典序 tuple（含 sandbox 三件套）
- 所有 repair role 共用 `build_repair_canonical_tools()`
- `build_repair_agent_prefix()`：L1 stable = rules + tools + examples；L2 role → `role_text`（不进 hash）
- `_tool_names: tuple[str, ...]` 字典序固定
- 执行权限仍由 `ToolGateway` 控制

## 不在范围

- `.agent/tools.yaml` manifest
- tools 段独立 budget / 分段 hash
