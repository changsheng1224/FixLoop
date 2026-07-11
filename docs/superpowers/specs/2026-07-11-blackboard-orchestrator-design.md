# Blackboard 接入 Orchestrator 主路径 — 设计规格

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §12.3 — Blackboard 与 Agent 通信
- **Layer:** L2
- **Primary modules:** `src/repair/blackboard_merge.py`, `src/repair/pipeline.py`, `src/repair/run_trace.py`, `src/state.py`
- **Acceptance:** `pytest tests/test_blackboard_merge.py tests/test_orchestrator.py -v`
- **Branch:** `V1.2-Bonus6-Multi-Agent`

## 方案 A（已实现，localize write）

Orchestrator 代理写入：

1. 每 repair 创建 `Blackboard()` 实例
2. Localize∥Retrieve 解析后 `write_localize_phase_to_blackboard`
3. trace：`blackboard_written` · `blackboard_snapshot`

**Patch 边界 merge** 见 `2026-07-11-blackboard-merge-for-patch-design.md`。

## Key 命名空间

| 前缀 | 示例 | 写入者 |
|------|------|--------|
| `suspect:` | `suspect:calc.py:42` | Localizer |
| `context:` | `context:related_tests` | Retriever |

## 不在范围

- Agent 直写 Blackboard（无 BB Tool）
- 线程锁（单线程 Orchestrator 写入）
- Patcher prompt 读 `read_related`（仍读 RepairState）
