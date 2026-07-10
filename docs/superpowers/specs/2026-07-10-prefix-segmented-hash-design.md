# Prefix 分段 hash 观测（Scheme A）

**日期**: 2026-07-10  
**状态**: 已实现  
**关联**: `docs/bonus.md` §3.2 P2 — prefix 分段 hash

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §3.2 — prefix 分段 hash [P2]
- **Layer:** L1
- **Primary modules:** `agent_runtime/prompt_prefix.py`, `agent_runtime/context_manager.py`, `agent_runtime/agent_loop.py`
- **Acceptance:** `pytest tests/test_prefix_stable.py tests/test_cache_and_dryrun.py -v`
- **Branch:** `V1.1-Bonus6-Context工程`

## 目标

在 **不改变** `prompt_cache_key` 语义的前提下，暴露 `prefix_hashes` 观测字段，便于 trace/report 判断哪一段 prefix 发生变化。

## 方案 A：观测型分段 hash

`build_prefix_hashes(prefix)` 返回：

| 键 | 来源 |
|----|------|
| `system` | SHA256(`stable_system_text`) |
| `tools` | SHA256(`stable_tools_text`) |
| `skills` | SHA256(`stable_skills_text`) |
| `cache_key` | `prefix.hash` = SHA256(system+tools) |
| `role` | SHA256(`role_text`)，L2 角色，不进 cache |
| `tool_signature` | 已有 schema 指纹 |
| `assets_fingerprint` | 外置 rules+examples 指纹 |
| `workspace_fingerprint` | workspace 快照指纹 |

## 接线

- `ContextManager._base_metadata()` → `metadata["prefix_hashes"]`
- `AgentLoop` `context_built` trace → `prefix_hashes`

## 不变量

- `metadata["prompt_cache_key"] == prefix_hashes["cache_key"]`
- skills/examples 变更只变 `skills` / `assets_fingerprint`，**不变** `cache_key`
- workspace 变更只变 `workspace_fingerprint`

## 范围外（Phase 2）

- Provider 多断点 `cache_control`（方案 B）
- `report.json` 聚合

## 验收

```bash
pytest tests/test_prefix_stable.py tests/test_cache_and_dryrun.py -v
```
