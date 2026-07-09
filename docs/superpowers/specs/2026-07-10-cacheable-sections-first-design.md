# 可缓存段前置 · 动态段后置（Scheme A）

**日期**: 2026-07-10  
**状态**: 已实现  
**关联**: `docs/bonus.md` §5.1 P1

## 目标

Prompt 填充顺序：`system (stable) → workspace → memory → relevant → history → request`。

- **文本解析路径**（`ContextManager.build`）：上述顺序拼接为单一 prompt。
- **Native tool API 路径**：`system_prompt = stable_text`；workspace + memory + relevant + history + task 进入 `user_message`。

## 不变量

- `PromptPrefix.hash` 仍仅基于 `stable_text`。
- `metadata.prompt_cache_key` 不变。
- `metadata.sections.prefix` 保留为 `system + workspace` token 之和（兼容旧消费方）。

## 范围外

- skills index、state/plan_todos 段
- Anthropic `cache_control` 块
- 八段 metadata 全量迁移
