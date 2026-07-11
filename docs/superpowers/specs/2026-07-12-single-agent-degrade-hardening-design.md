# Single-Agent 降级加固 — 设计规格

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §10.2 P2 — Multi-Agent 降级 Single-Agent
- **Layer:** L2
- **Primary modules:** `src/repair/degrade.py`, `src/repair/baseline_apply.py`, `src/cli.py`
- **Acceptance:** `pytest tests/test_repair_degrade.py -v`
- **Branch:** `V1.2-Bonus6-Multi-Agent`

## 方案 A（加固，已实现）

在现有 `run_baseline_fallback` 上增强：

1. Blackboard 前缀块注入降级 prompt
2. L2 `agent_asks` 记录 baseline phase=degrade
3. 降级后 pytest 复验（`mark_fixed_on_apply=False`）
4. `allow_baseline_degrade` / CLI `--no-degrade`
5. report `degraded_mode` / `degraded_trigger`；trace `baseline_verify_finished`

## 不在范围

- 多触发 DegradePolicy（localize 空等）
- 委托 `SingleAgentOrchestrator`
