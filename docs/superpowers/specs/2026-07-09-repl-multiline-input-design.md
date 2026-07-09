# L1 REPL 多行输入 — 设计规格

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §21 — 多行输入 `\` 续行 [P2]
- **Layer:** L1
- **Primary modules:** `agent_runtime/repl_input.py`, `agent_runtime/cli.py`, `tests/test_repl_input.py`
- **Acceptance:** `pytest tests/test_repl_input.py -v`
- **Branch:** `V1.1-Bonus3-CLI-REPL`

## 目标

L1 REPL 支持行尾 `\` 续行，便于粘贴堆栈与多段 prompt；`/command` 保持单行。

## 方案 A

- `line_ends_with_continuation(line)`：行尾奇数个 `\` 表示续行
- `read_repl_input()`：首行 `> `，续行 `... `；`reader` 可注入
- 首行以 `/` 开头 → 禁用续行
- `_repl_mode` 调用 `read_repl_input()` 替代 `input().strip()`

## 不在范围

- readline 历史（§21 P1）
- `src.cli repair --issue` 多行
- HITL `input()` 多行

## 测试

- 单行、多行续行、`\\` 字面量、`/cmd\` 不续读、续行中间空行（单独 `\` 行）
