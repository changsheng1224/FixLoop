# few-shot / rules 外置 — 设计规格

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §5.1 — few-shot / rules 外置 [P2]
- **Layer:** L1
- **Primary modules:** `agent_runtime/prompt_external.py`, `agent_runtime/prompt_prefix.py`, `agent_runtime/runtime.py`, `agent_runtime/checkpoint.py`
- **Acceptance:** `pytest tests/test_prompt_external.py tests/test_prompt_prefix.py tests/test_prefix_stable.py -v`
- **Branch:** `V1.1-Bonus4-Prompt`

## 方案 A + A1（已实现）

- Repo 覆盖：`{repo_root}/.agent/rules.md`、`.agent/examples.md`
- 缺失时 Python 内置 default（与改前行为等价）
- `compose_rules()` 追加 dry_run / approval 运行时后缀
- `PromptPrefix.assets_fingerprint` + checkpoint identity
- 外置内容进入 stable_text → 变更自动 invalidate `prompt_cache_key`

## 不在范围

- `_system_persona` 外置
- 会话 mid-flight 热加载
- Jinja 模板化
