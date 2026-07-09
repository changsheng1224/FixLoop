# Tool Gateway Dispatch — Design Spec

**Date:** 2026-07-10  
**Status:** Implemented (Scheme A)  
**Refs:** `docs/bonus.md` §7.1 P1, `docs/bonus/DESIGN.md` §7.4  
**Branch:** `V1.1-Bonus5-Tool-Gateway`

## Problem

Repair 流水线虽注入 `tool_policy=gw.can_call`，但权限检查在 `ToolExecutor` 内联复制，`ToolGateway.dispatch()` 未被强制为唯一入口。模型侧 prompt/API 理论上只应消费 schema，registry 仍含 `run` 指针。

## Solution (Scheme A)

1. **`Agent.tool_dispatch`** — factory 注入 `gw.dispatch`（替代 `tool_policy`）
2. **`Agent.execute_tool`** — 外层 `dispatch(agent, tool, fn)`，`fn` 内调 `ToolExecutor.execute_gated`
3. **`ToolExecutor.execute_gated`** — Gate 1–9，无 Gateway 层
4. **`tool_schema_view(registry)`** — prompt / native API 仅读 `{schema, description, risky}`

## Schema visibility

维持 Bonus4 策略：**可见 canonical 14-tool schema**，执行拒绝在 Gateway。

## Verification

```bash
pytest tests/test_gateway_dispatch.py tests/test_tool_schema.py tests/test_middleware.py tests/test_agents_m5.py tests/test_tool_executor.py -v
```
