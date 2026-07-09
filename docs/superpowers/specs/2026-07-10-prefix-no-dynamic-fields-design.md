# prefix 禁动态字段 — 设计规格

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §5.1 — prefix 禁动态字段 [P1]
- **Layer:** L1
- **Primary modules:** `agent_runtime/prefix_stable.py`, `agent_runtime/prompt_prefix.py`, `agent_runtime/runtime.py`
- **Acceptance:** `pytest tests/test_prefix_stable.py tests/test_prompt_prefix.py tests/test_cache_and_dryrun.py -v`
- **Branch:** `V1.1-Bonus4-Prompt`

## 目标

稳定段（persona + rules + tools + examples / L2 system_prompt）禁止 timestamp、run_id、session_id、nonce；`prompt_cache_key` 仅 hash 稳定段，workspace 变更不 bust hash。

## 方案 A

- `assert_stable_prefix_clean()` + `hash_stable_prefix()`
- `PromptPrefix.stable_text`；移除 `built_at`
- `build_custom_system_prefix()` 修复 L2 `hash=""`

## 不在范围

- workspace fingerprint 降噪（P2）
- 八段 prefix 拆分
