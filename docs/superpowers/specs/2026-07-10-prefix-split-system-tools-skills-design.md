# Prefix 物理拆段 system/tools/skills（Scheme A）

**日期**: 2026-07-10  
**状态**: 已实现  
**关联**: `docs/bonus.md` §3.2 P2 — system/tools/skills 拆 prefix

## 目标

`PromptPrefix` 构建期产出 `stable_system_text` / `stable_tools_text` / `stable_skills_text`；
`ContextManager` 独立 fill；stable 段超 cap **整段丢弃**（不 splice）。

## Hash

`prompt_cache_key` = SHA256(system + tools)；skills/examples 变更不 bust cache。

## 验收

```bash
pytest tests/test_prompt_prefix.py tests/test_context_section_order.py \
       tests/test_context_projection.py tests/test_prefix_stable.py -v
```
