# write_file 原子写 — 设计规格

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §6.1 — write_file 原子写 [P1]
- **Layer:** L1
- **Primary modules:** `agent_runtime/atomic_io.py`, `agent_runtime/tools.py`
- **Acceptance:** `pytest tests/test_write_patch.py -v`
- **Branch:** `V1.1-Bonus2-Agent-Tool`

## 目标

`write_file` 覆盖写与 append 均通过「同目录临时文件 → `Path.replace`」完成，进程崩溃或写入失败时不留下半截目标文件。

## 方案

抽取 `atomic_write_text(path, content)`；`tool_write_file` 调用之。

- **覆盖写 / 新文件：** 直接原子写 `content`
- **append 且文件存在：** 读旧内容拼接后原子 replace（全程原子）

临时文件命名：`{stem}{suffix}.tmp`（如 `calc.py` → `calc.py.tmp`）。

## 不在范围

- `patch_file` 原子写（独立 backlog）
- 重构 `session_store` / `run_store`

## 测试

- 正常写入、覆盖、append、建目录（既有）
- 无 `.tmp` 残留
- `replace` 失败时原文件不变、tmp 清理
