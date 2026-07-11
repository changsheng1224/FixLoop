# L1/L2 State 关联字段 — 设计规格

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §12.2 P2 — L1/L2 State 关联字段
- **Layer:** L1 + L2
- **Primary modules:** `src/repair/l2_binding.py`, `agent_runtime/task_state.py`, `src/state.py`, `src/repair/pipeline.py`, `agent_runtime/agent_loop.py`
- **Acceptance:** `pytest tests/test_l2_binding.py tests/test_l2_state_binding.py -v`
- **Branch:** `V1.2-Bonus6-Multi-Agent`

## 方案 A（已实现）

- `TaskState` 增 `l2_repair_run_id` / `l2_agent` / `l2_phase` / `l2_attempt`
- `RepairState.agent_asks[]` 登记每次 ask 或 synthetic 调用
- Orchestrator `_begin_l2_agent_ask` / `_finish_l2_agent_ask` / `_record_l2_synthetic_ask`
- trace 事件 `agent_ask_started` / `agent_ask_finished`；L1 `_emit` 默认注入 L2 字段
- Patcher `complete_once`、Verifier sandbox 走 synthetic 路径
- Phase 1：`task_state.{agent}.json` 仍覆盖（retry 不保留历史文件）

## 不在范围

- `run_id` 写入 prompt prefix
- 每 attempt 独立 task_state 文件（Phase 2）
