# Plan: 统一 Token 会计（Session → report.json）

## FixLoop Context

- **Bonus ref:** `docs/bonus.md` §1 — 统一 token 会计；字段规范 `§19.4`
- **Layer:** L1（主）+ L2 汇总接线
- **Primary modules:**
  - `agent_runtime/token_accounting.py`（新建）
  - `agent_runtime/providers/clients.py`
  - `agent_runtime/agent_loop.py`
  - `src/eval/token_usage.py`
  - `src/repair/run_trace.py`
- **Acceptance:** `pytest tests/test_token_usage.py tests/test_agent_loop.py -v`；repair 后 `.agent/runs/*/report.json` 含 cache 字段
- **Branch:** `V1.1-Bonus1-Agent运行时`

## 背景与范围

PR #86 已落地 per-agent token、repair trace、`build_repair_token_usage`。本任务**不重写 trace**，补齐 DESIGN §19.4 Gap：

| 字段 | 现状 | 目标 |
|------|------|------|
| `input_tokens` / `output_tokens` / `api_calls` | ✅ client + report | 保持 |
| `token_usage`（context sections） | ✅ L1 估算 | 保持，标注 `source` |
| `cache_read_tokens` / `cache_creation_tokens` | ❌ | session 累加 |
| `cache_hit_rate` | ❌ | report 写入时计算 |
| session 级 repair 汇总 | 部分（多 client 相加） | 统一 schema + by_agent |

**不在本 PR：** TTFT、八段 `context_sections`、Prometheus、eval 矩阵聚合（§19.4 / §20.3 后续项）。

## 设计

### 1.  canonical schema — `TokenUsageSnapshot`

```python
@dataclass
class TokenUsageSnapshot:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    api_calls: int = 0
    # 可选：context 估算（与 API 分离）
    estimated_total: int = 0
    estimated_sections: dict = field(default_factory=dict)
    source: str = "api"  # api | context_estimate | merged
```

`to_report_dict()` 输出 report.json 顶层字段；`cache_hit_rate` 由 `compute_cache_hit_rate(read, creation)` 派生。

### 2. ModelClient session_usage 扩展

`FakeModelClient` / `AnthropicCompatibleClient`：

- `session_usage` 增加 `cache_read_tokens`、`cache_creation_tokens`
- `_record_usage(usage)` 从 Provider `usage` 解析：
  - Anthropic: `cache_read_input_tokens`, `cache_creation_input_tokens`
  - DeepSeek/OpenAI 兼容: `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`（按实际响应字段映射，缺失则 0）
- `reset_session_usage()` 一并清零

### 3. L1 AgentLoop → report.json

`_finalize_run`：

- 从 `agent.model_client.session_usage` 读取 API 累计（含 cache）
- 与 `_last_token_meta`（context 估算）合并：`merge_usage(api, context_meta)`
- `report_body` 增加：`cache_read_tokens`, `cache_creation_tokens`, `cache_hit_rate`

### 4. L2 repair 汇总

`src/eval/token_usage.py`：

- `get_client_session_usage` 返回 cache 字段
- `build_repair_token_usage` 跨 client 累加 cache；`agent_report.*.json` 透传
- `RepairRunTracer.finalize` 写入 repair 级 `report.json`（已有 `**token_summary`）

### 5. 测试

| 文件 | 用例 |
|------|------|
| `tests/test_token_accounting.py` | merge、cache_hit_rate、空 cache |
| `tests/test_token_usage.py` | 扩展 session cache 累加、repair 汇总 |
| `tests/test_agent_loop.py` | finalize report 含 cache 字段（FakeClient 模拟 usage） |

## 任务清单

1. [x] 新建 `agent_runtime/token_accounting.py`（~80 行）
2. [x] 扩展 `clients.py` session_usage + usage 解析（~40 行）
3. [x] `agent_loop._finalize_run` 接线（~25 行）
4. [x] `token_usage.py` 汇总 cache（~30 行）
5. [x] 单测（~120 行）
6. [ ] 本地 `pytest tests/ -v`（PR 前全量）

## 预估 diff

~300 行（小～中），无破坏性 API 变更。
