# Blackboard 前缀订阅 — 设计规格

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §12.3 P2 — 前缀订阅
- **Layer:** L2
- **Primary modules:** `src/repair/blackboard_subscribe.py`, `src/prompts/patcher_task_builder.py`
- **Acceptance:** `pytest tests/test_blackboard_subscribe.py tests/test_orchestrator.py -v`
- **Branch:** `V1.2-Bonus6-Multi-Agent`

## 方案 A（已实现）

- `PrefixSubscription` + `PATCHER_PREFIX_SUBSCRIPTIONS`（`suspect:` / `context:` / `scratch:`）
- `subscribe_prefixes` → batch `read_related`
- `render_patcher_prefix_blocks` → suspects_block / test_blocks / scratch_block
- `assemble_patcher_variables(blackboard=...)` 替代手工拼块
- trace：`blackboard_prefix_subscribed`

## 不在范围

- Localizer/Verifier 订阅表
- `subtask:` 命名空间（§12.8 后续）
