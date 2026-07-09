# repair 退出码 — 设计规格

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §21 — repair 退出码 [P1]；§25 — CLI 退出码单测 [P1]
- **Layer:** L2
- **Primary modules:** `src/cli_exit_codes.py`, `src/cli.py`, `tests/test_cli_exit_codes.py`, `tests/test_cli_repair.py`
- **Acceptance:** `pytest tests/test_cli_exit_codes.py tests/test_cli_repair.py -v`
- **Branch:** `V1.1-Bonus3-CLI-REPL`

## 目标

`src.cli repair` 按 `RepairState` 与启动前配置返回规范退出码，供 CI/脚本判断成败，不再依赖解析 stdout。

## 退出码

| 码 | 含义 | 判定 |
|----|------|------|
| 0 | 成功 | `status=fixed`，或 `status=patched` 且有 `candidate_patches` |
| 1 | 修复失败 | `failed` / `exhausted` / 无补丁 / 其他非成功终态 |
| 2 | 配置错误 | `--repo` 不存在；未设置 `DEEPSEEK_API_KEY`；工厂/装配异常 |
| 3 | 超时 | `node_timings.repair_timeout` 或 `agent_errors.orchestrator` 含 `repair timeout` |

超时判定优先于通用失败（1）。

## 方案 A

- `src/cli_exit_codes.py`：`REPAIR_EXIT_*` 常量、`repair_exit_code(state)`、`repair_config_error(repo)`
- `_repair()`：启动前 `repair_config_error` → 2；`try/except` 工厂异常 → 2；结束后 `repair_exit_code(state)`

## 不在范围

- L1 REPL 退出码
- `eval` / `ablation` 退出码改动
- CLI `--repair-timeout` 接线
- 运行时 API 401 映射为 2

## 测试

- `test_cli_exit_codes.py`：映射表 + 配置预检
- `test_cli_repair.py`：成功 0、失败 1、无 key 2、超时 3
