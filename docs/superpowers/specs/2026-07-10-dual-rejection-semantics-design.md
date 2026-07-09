# Dual Rejection Semantics — Design Spec

**Date:** 2026-07-10  
**Status:** Implemented (Scheme A)  
**Refs:** `docs/bonus.md` §7.2 P2, `docs/bonus/DESIGN.md` §7.3, §19.3  
**Branch:** `V1.1-Bonus5-Tool-Gateway`

## Problem

双层闸（`fa914b6`）仅覆盖 Gateway 与 Gate 7 的 `rejection_layer`；Executor Gate 1–6 拒绝无统一语义，report/trace 无法聚合拒绝分布。

## Solution (Scheme A)

### Metadata normalization

- `build_executor_rejection_metadata(gate_id, code)` / `ToolExecutor._rejected()`
- 全部 Executor 拒绝带 `rejection_layer: "executor"` + `gate_id`
- Gate 9 异常带 `rejection_layer: "executor"`, `gate_id: 9`, `tool_status: "error"`
- Gateway 增加 `rejection_reason: "role_not_allowed"`

### TaskState aggregation

- `record_tool_rejection(tool, metadata)` 累计三层 dict
- `rejection_report_fields()` → report.json

### Report fields

```json
{
  "tool_rejections_by_layer": {"gateway": 2, "executor": 1},
  "tool_rejections_by_gate": {"gateway": 2, "7": 1},
  "permission_denied_by_tool": {"write_file": 2}
}
```

## Acceptance

```bash
pytest tests/test_rejection_semantics.py tests/test_dual_gate.py \
  tests/test_tool_executor.py tests/test_middleware.py -v
```

## Deferred

- History `tool_metadata` 持久化（Scheme B）
- L2 `RepairState.agent_errors` gateway 累计
