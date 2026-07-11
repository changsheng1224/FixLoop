# 分阶段 Repair 超时 — 设计规格

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §12.1 — 分阶段超时 [P1]
- **Layer:** L2
- **Primary modules:** `src/repair/phase_clock.py`, `src/repair/pipeline.py`, `src/orchestrator.py`
- **Acceptance:** `pytest tests/test_phase_clock.py tests/test_phase_timeout.py -v`
- **Branch:** `V1.2-Bonus6-Multi-Agent`

## 方案

**Scheme A — RepairPhaseClock**：Orchestrator 持阶段预算时钟；`localize`（L∥R 墙钟）、`patch`、`verify` 跨 retry **累计**；`repair_total_s` 硬顶与现有 `repair_timeout_s` 对齐。

## 默认预算

| 阶段 | 秒 | 说明 |
|------|-----|------|
| localize | 60 | Localizer∥Retriever 并行墙钟 |
| patch | 90 | 所有 retry 累计 |
| verify | 120 | 所有 retry 累计 |
| repair_total | = `repair_timeout_s` | 默认 180；`repair_timeout_s≤0` 时全部禁用 |

## 超时行为

- `cancel_token.cancel("timeout")`
- `restore_repo_snapshot(initial_snapshot)`
- `status=timeout`，`node_timings.phase_timeout`，trace `phase_timeout`
- 不误标 `user_cancel`

## API

```python
orch.repair(issue, repair_timeout_s=180, phase_timeouts=PhaseTimeoutConfig(...))
```

`phase_timeouts=None` → `PhaseTimeoutConfig.from_repair_timeout(repair_timeout_s)`。
