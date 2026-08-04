# Langfuse 与 Prometheus（实现链路）

> Canonical Trace 之上的第三方可观测适配。  
> **代码权威目录**：`agent_runtime/observability/`。计划勾选见 `docs/2026-08-03-to-08-09-enhancement-plan.md`（8 月 4 日｜Langfuse 与 Prometheus）。

---

## 1. 问题与边界

### 1.1 要解决什么

1. 把本地 Canonical Trace 信封导出到 **Langfuse**，可查看模型 / Tool / Skill / Context / 状态迁移轨迹  
2. 用 **Prometheus** 暴露成功率、阶段延迟、Token、重试、错误等指标，且与 Trace **口径一致**  
3. Label **仅低基数**；Exporter 失败 **不阻塞** 主任务与本地 JSONL  

### 1.2 明确不做

- 不引入 `langfuse` / `prometheus_client` Python SDK 作为运行时依赖  
- 不把 `run_id` / `user_id` / `issue_id` 写入 Prometheus Label  
- 不要求线上必须连 Langfuse Cloud（默认关闭，有 Key 才导出）  

### 1.3 在系统中的位置

```text
Repair / AgentLoop / L2 Ask
        │
        ▼
RunStore.append_trace_event
        │  enrich + JSONL 落盘（主路径）
        ▼
observability.after_trace_append  ──fail-soft──┐
        │                                       │
        ├─► prom_from_trace → MetricsRegistry → GET /metrics
        └─► langfuse_exporter → HTTP /api/public/ingestion
```

---

## 2. 能力全景

| 能力 | 说明 |
|------|------|
| Label 守卫 | `labels.py`：禁止高基数字段；白名单低基数 |
| Trace→Prom | 事件类别 / Skill / Error / Model 计数 |
| Repair 补齐 | `token_usage_total`、`cache_hit_rate` 在 `_push_repair_metrics` 接线 |
| Langfuse 映射 | `trace_id`→Trace；每事件→span/generation；首事件 `trace-create` |
| 脱敏 | 导出前 `redact_artifact` |
| Fail-soft | 钩子与注册表更新均 `try/except` |
| Endpoint | 既有 `start_metrics_server` → `/metrics` + Grafana JSON |

---

## 3. 环境变量

| 变量 | 作用 |
|------|------|
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | 有则默认开启导出 |
| `LANGFUSE_HOST` | 默认 `https://cloud.langfuse.com` |
| `FIXLOOP_LANGFUSE_ENABLED` | `0` 强制关；`1` 强制开（仍需 Key） |
| `FIXLOOP_LANGFUSE_TIMEOUT_SEC` | HTTP 超时，默认 5 |
| `FIXLOOP_METRICS_VERSION` | Prometheus `version` Label，默认 `1` |
| `FIXLOOP_METRICS_PORT` | `/metrics` 端口（既有） |

---

## 4. Prometheus Label 规则

**禁止**：`run_id`、`user_id`、`issue_id`、`trace_id`、`span_id`、`task_id`、`path` 等（见 `FORBIDDEN_LABEL_KEYS`）。

**允许（低基数）**：`model`、`phase`、`skill`、`status`、`version`、`tier`、`event_category`，以及既有 Intent 枚举字段。

`MetricsRegistry.counter_inc` / `gauge_*` 入口自动 `strip_forbidden_labels`。

### 新增 / 对齐指标

| 名称 | 来源 |
|------|------|
| `fixloop_trace_events_total` | Canonical 每事件 |
| `fixloop_skill_matched_total` | `skill_matched` |
| `fixloop_errors_total` | `status=error` / cancel |
| `fixloop_model_events_total` | `model_*` |
| `fixloop_token_usage_total` | repair finalize（补齐） |
| `fixloop_cache_hit_rate` | repair finalize（补齐） |
| `fixloop_repair_status` / `phase_ms` / `retry_count` | 既有 |

---

## 5. Langfuse 映射

| Canonical | Langfuse |
|-----------|----------|
| `trace_id`（v1=`run_id`） | Trace `id` |
| 首条事件 | `trace-create` |
| `model_*` | `generation-create` |
| 其它事件 | `span-create` |
| `span_id` / `parent_span_id` | metadata + `parentObservationId` |
| `seq` | observation `id` = `{trace_id}:{seq}` |

验收：配置 Key 后跑一次 repair，在 Langfuse UI 可见完整 Trace；无 Key 时本地 JSONL 与 `/metrics` 仍可用。

---

## 6. 测试

```bash
pytest tests/test_langfuse_exporter.py tests/test_prom_label_guard.py tests/test_prometheus_metrics.py -v
```

覆盖：映射、样本 JSONL 导出、高基数剥离、Exporter 失败不丢 JSONL、payload 脱敏。

---

## 7. 一句话总结

`agent_runtime/observability`，Canonical Trace → Langfuse HTTP 适配 + 低基数 Prometheus 指标（高基数保护 / fail-soft / 脱敏）。
