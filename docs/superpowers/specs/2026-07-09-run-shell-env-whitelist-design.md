# run_shell 环境变量白名单 — 设计规格

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §6.1 — run_shell 环境变量白名单 [P2]
- **Layer:** L1
- **Primary modules:** `agent_runtime/security.py`, `tools.py`, `tool_context.py`, `tool_executor.py`
- **Acceptance:** `pytest tests/test_shell_security.py -v`
- **Branch:** `V1.1-Bonus2-Agent-Tool`

## 目标

加固既有 `shell_env` 白名单，与 §18 联动：`run_shell` 子进程仅见固定白名单变量；输出经 `redact_text` 脱敏。

## 实现要点

- 严格固定 `SHELL_ENV_WHITELIST`（含 Windows 常用项），不可运行时扩展
- `looks_sensitive_env_name` 二次过滤键名
- 强制 `PYTHONIOENCODING=utf-8`；`PWD=workspace`
- `build_tool_registry` 注入 `shell_env_provider`
- `tool_run_shell` 经 provider 取 env；返回 `redact_text(...)`
- trace metadata：`shell_env_keys`（仅键名）

## 不在范围

- 命令黑名单、可配置扩展白名单、L2 沙箱
