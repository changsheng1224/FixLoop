# 多模型 Tokenizer 切换（Scheme A）

**日期**: 2026-07-10  
**状态**: 已实现  
**关联**: `docs/bonus.md` §3.2 P1 — 多模型 tokenizer 切换

## 目标

按 `model` + `provider` 选择 tokenizer backend，未知模型 fallback 到 `cl100k_base` 并 `log.warning`；metadata 暴露 `tokenizer_backend` / `tokenizer_fallback` / `tokenizer_id`。

## 方案 A

- 新增 `agent_runtime/tokenizer_registry.py`：`TokenRule` 显式表
- 匹配顺序：**exact model → prefix → provider → global fallback**
- `resolve_token_counter()` 查表构建 `TiktokenCounter` / `HuggingFaceTokenizerCounter`
- `fit_prompt_to_budget()` metadata 补充 `tokenizer_fallback`、`tokenizer_id`
- L2：`fit_repair_user_prompt()` 用于 patcher 手工 prompt；repair trace 记录各 agent tokenizer 解析

## Anthropic / Claude v1

`cl100k_base` 近似计数 + `warn="approximate_tokenizer"`。

## 验收

```bash
pytest tests/test_tokenizers.py tests/test_tokenizer_local.py tests/test_context_manager.py -v
```

## 范围外（Phase 3）

- YAML 外置 `tokenizer_map.yaml`
- Claude 专用 HF 词表
