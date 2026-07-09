# patch_file 多 hunk 预览 — 设计规格

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §6.1 — patch_file 多 hunk 预览 [P1]
- **Layer:** L1
- **Primary modules:** `agent_runtime/patch_engine.py`, `tools.py`, `tool_executor.py`, `agent_loop.py`
- **Acceptance:** `pytest tests/test_patch_engine.py tests/test_write_patch.py tests/test_tool_executor.py -v`
- **Branch:** `V1.1-Bonus2-Agent-Tool`

## 目标

`patch_file` 支持 unified diff 多 hunk apply；apply 前生成 diff 摘要，进入审批提示与 `tool_preview` trace。

## 实现要点

- `PatchFileArgs.diff` 可选；与 `old_text`/`new_text` legacy 路径互斥
- `patch_engine.py`：解析、预览、`apply_plan`（对齐 eval patch_utils hunk 语义）
- `tool_patch_file`：`atomic_write_text` 写盘
- `ToolExecutor` Gate 6.5：预览校验；Gate 7：审批展示 `preview_text`
- `AgentLoop`：`tool_preview` → `tool_executed` 顺序写入 trace

## 不在范围

- 多文件 diff、`write_file` 审批预览、L2 PatchApplier 改造
