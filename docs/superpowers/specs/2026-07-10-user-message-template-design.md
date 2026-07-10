# User Message 模板化（Scheme A）

**日期**: 2026-07-10  
**状态**: 已实现  
**关联**: `docs/bonus.md` §3.2 P2 — User Message 模板化

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §3.2 — User Message 模板化 [P2]
- **Layer:** L1 + L2
- **Primary modules:** `agent_runtime/user_message_template.py`, `agent_runtime/context_manager.py`, `src/prompts/repair_tasks.py`, `src/prompts/tasks/*.md`
- **Acceptance:** `pytest tests/test_user_message_template.py tests/test_repair_tasks.py tests/test_context_manager.py tests/test_orchestrator.py -v`
- **Branch:** `V1.1-Bonus6-Context工程`

## 方案 A：Stdlib Template + 外置文件

- L1：`string.Template` + 默认 `## 当前任务\n\n$task`；可选 `.agent/task_template.md`
- L2：`src/prompts/tasks/{role}.md`；列表/条件块由 Python 预渲染为 `$变量`
- `metadata.task_template_source` + `task_template_fingerprint`
- 不引入 Jinja2

## 验收

```bash
pytest tests/test_user_message_template.py tests/test_repair_tasks.py \
       tests/test_context_manager.py tests/test_orchestrator.py tests/test_prompt_loader.py -v
```
