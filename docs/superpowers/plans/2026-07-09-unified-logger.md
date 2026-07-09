# Plan: 统一 Logger + --log-level（方案 B）

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §1 — 统一 logger + `--log-level`
- **Layer:** L1 + L2 CLI / repair 主路径
- **Primary modules:** `agent_runtime/logging_setup.py`, `agent_runtime/cli.py`, `src/cli.py`, `src/repair/pipeline.py`, `src/orchestrator.py`, `agent_runtime/agent_loop.py`
- **Acceptance:** `pytest tests/test_logging_setup.py -v`；`repair --log-level WARNING` 抑制 DEBUG
- **Branch:** `V1.1-Bonus1-Agent运行时`

## 范围

- ✅ `configure_logging` / `get_logger` / `resolve_log_level`
- ✅ L1 `agent_runtime` + L2 `src.cli` + `src.eval` `--log-level`
- ✅ `--verbose` → DEBUG（显式 `--log-level` 优先）
- ✅ `FIXLOOP_LOG_LEVEL` 环境变量
- ✅ pipeline / orchestrator / agent_loop 迁移
- ❌ JSON handler（§19.1 后续）
- ❌ `CLIProgressCallback` 彩色输出（Phase 1 保留）
