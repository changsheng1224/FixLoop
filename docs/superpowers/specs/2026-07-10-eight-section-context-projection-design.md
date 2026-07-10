# 八段 Context 投影 Schema（Scheme A）

**日期**: 2026-07-10  
**状态**: 已实现  
**关联**: `docs/bonus.md` §3.2 P1 — 八段 Context 投影 schema

## 目标

在保留现有 `metadata.sections`（五/六 section 实现层）前提下，双写八段语义键 `context_sections`：

`system / task / state / knowledge / tools / skills / memory / history`

## 方案 A：Dual-Write + Mapper

- 新增 `agent_runtime/context_projection.py`
- `ContextManager._fill_sections()` 末尾 attach 八段投影
- `fit_prompt_to_budget()` 同步 attach（L2 fit 路径）
- `AgentLoop` 发射 `context_built` trace 事件

## 映射规则（Phase 1）

| 八段 | 来源 |
|------|------|
| system | stable 非 tools/examples + workspace |
| tools | stable `## 可用工具` 段 |
| skills | stable `## 调用示例` + role |
| memory | sections.memory |
| knowledge | sections.relevant |
| state | session.plan_todos（无则 0） |
| history | sections.history |
| task | sections.request |

## 验收

```bash
pytest tests/test_context_projection.py tests/test_context_manager.py -v
```

## 范围外（Phase 2+）

- PromptPrefix 物理拆段
- state 从 TaskState 全量注入 build
- JSON Schema 校验（方案 C）
