# Blackboard merge-for-patch — 设计规格

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §12.3 — merge 阶段读 Blackboard
- **Layer:** L2
- **Primary modules:** `src/repair/blackboard_merge.py`, `src/repair/pipeline.py`, `src/orchestrator.py`
- **Acceptance:** `pytest tests/test_blackboard_merge.py tests/test_orchestrator.py -v`
- **Branch:** `V1.2-Bonus6-Multi-Agent`

## 方案 A（已实现）

1. Localize 后 **只 write** BB（`blackboard_written`）
2. 每次 `_run_patcher` 前 **`merge_blackboard_for_patch`**：`read_related` + `resolve_blackboard_conflicts`
3. Patcher prompt 仍经 `RepairState` 字段（物化自 BB）
4. Verify 失败 → `scratch:feedback` TTL 300s
5. trace：`blackboard_merge_for_patch`

## 冲突策略

- 同 key 异 source → `prefer_localizer`（`apply_conflict_winner`）
- 同 file+line → 保留高 `confidence`

## 不在范围

- Patcher 模板直接拼 BB raw KV
- Retriever 写 `suspect:*`
