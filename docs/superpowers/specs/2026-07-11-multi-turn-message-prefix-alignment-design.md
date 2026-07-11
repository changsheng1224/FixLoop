# 多轮 messages 前缀对齐 — 设计规格（Scheme C Phase 1）

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §5.1 P2 — 多轮 messages 前缀对齐
- **Layer:** L1 (`agent_runtime/`)
- **Primary modules:** `message_projection.py`, `context_manager.py`, `agent_loop.py`
- **Acceptance:** `pytest tests/test_message_projection.py tests/test_context_manager.py tests/test_agent_loop.py -v`
- **Branch:** `V1.2-Bonus5-Prompt`

## 目标

ReAct 多轮循环中，每 step 从 canonical `session["history"]` **全量投影**；已封印段的 prompt 前缀 byte-identical，禁止 divergent message 序列。

## 方案 C Phase 1

### 核心机制

1. **Run 级冻结**：`init_run_projection()` 快照 `memory` + 固定 `_run_user_query`（relevant 检索用）
2. **History 单调封印**：`_sealed_history_count` + `_sealed_history_text`；仅对 `history[sealed_count:]` 跑 L0–L5
3. **前缀断言**：`prefix_aligned = current_prefix.startswith(previous_prefix)`
4. **XML 接线**：`_xml_call_model` 传 `prompt_cache_key`
5. **Trace**：`projection_step` · `sealed_history_count` · `prefix_aligned` · `prefix_fingerprint`

### 不在范围（Phase 2）

- Anthropic multi-block `cache_control` / messages API 重构
- Native 每 turn 从 canonical 重建 messages[]
