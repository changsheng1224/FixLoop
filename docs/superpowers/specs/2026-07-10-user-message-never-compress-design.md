# User Message 永不压缩 Enforce（Scheme A）

**日期**: 2026-07-10  
**状态**: 已实现  
**关联**: `docs/bonus.md` §3.5 P1 — user message 永不压缩 enforce

## 方案 A：Reserve-first

1. 先计数 task/request 全文，预留 `section_cap = total - request_tokens`
2. 其他 section 仅在 `section_cap` 内填充/裁剪
3. **永不** `budget.fit()` request/user
4. `fit_prompt_to_budget(preserve_user=True)` 仅裁 system
5. `task_budget_overflow` 当 request 单独超过 total

## 边界

- `apply_l1_to_request_text` 仍只裁工具结果块（非 canonical issue）
- L0–L5 作用于 history，不修改 canonical jsonl

## 验收

```bash
pytest tests/test_task_preservation.py tests/test_context_manager.py tests/test_tokenizers.py -v
```
