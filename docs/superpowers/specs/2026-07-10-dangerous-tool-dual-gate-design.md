# Dangerous Tool Dual Gate — Design Spec

**Date:** 2026-07-10  
**Status:** Implemented (Scheme A)  
**Refs:** `docs/bonus.md` §7.1 P2, `docs/bonus/DESIGN.md` §7.3, §8  
**Branch:** `V1.1-Bonus5-Tool-Gateway`

## Problem

Bonus5 强制 `execute_tool → ToolGateway.dispatch → ToolExecutor.execute_gated`，但危险工具双层防御未完整可观测：

1. Gateway 拒绝缺少 `rejection_layer` 语义
2. Repair factory 固定 `approval="auto"`，Gate 7 存在但 trace 不可见
3. `run_shell` 权限表意图未文档化（实际为 default-deny，但注释误导）

## Solution (Scheme A)

### Layer 1 — ToolGateway

- 显式 `"run_shell": set()`：multi-agent repair 禁止宿主机 shell
- `permission_denied` metadata 增加 `rejection_layer: "gateway"`
- 移除误导性 `"*": {"*"}` 表项；未列出工具 default-deny

### Layer 2 — Executor Gate 7

- `approval_denied` 增加 `rejection_layer: "executor"`, `gate_id: 7`, `approval_policy`
- 高风险工具成功时记录 `gate_id`, `approval_policy`, `approval_result`（`auto_allowed` / `user_approved`）

### Factory

- `create_repair_agent(..., approval="auto")` 参数化；headless repair 默认 auto，Layer 1 为主防御

### Trace

- `agent_loop._emit_tool_trace` 透传安全相关 metadata 至 `tool_executed` 事件

## Acceptance

```bash
pytest tests/test_dual_gate.py tests/test_agents_m5.py tests/test_gateway_dispatch.py \
  tests/test_tool_executor.py tests/test_middleware.py -v
```

| 场景 | 期望 |
|------|------|
| Localizer → write_file | L1 permission_denied |
| Patcher → run_shell | L1 permission_denied |
| Patcher → write + never | L2 approval_denied gate 7 |
| Patcher → write + auto | 成功 + gate_id 7 auto_allowed |
| Baseline → run_shell | L1 通过 + Gate 7 auto |

## Deferred

- §7.2 permission_denied 审计计数
- §8 P2 Gate 7 分级审批（read auto / write ask 独立于 global policy）
- §8 P1 write_file 审批 diff 预览
