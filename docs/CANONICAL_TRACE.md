# Canonical Trace（实现链路）

> 统一 FixLoop 运行事件信封与 Span 父子关系，使任意 `run_id` 可还原执行顺序与阶段树。  
> **权威决策**：`docs/design-decisions.md` ADR-011；代码：`agent_runtime/canonical_trace.py`。  
> **本切片不含** Langfuse / Prometheus 适配（周计划功能2）。

---

## 1. 问题与边界

### 1.1 要解决什么

1. 多 Agent 共享 `trace.jsonl` 时缺少统一顶层字段与 Span 树  
2. 需要按 `run_id` 稳定还原时间线（含同毫秒事件）  
3. 为后续 Langfuse 导出提供本地契约  

### 1.2 明确不做

- 不改 ADR-009 的 JSONL 追加模型  
- 不强制重命名既有 `event` 字符串  
- 不在本切片接入 Langfuse/OTel exporter  

### 1.3 在系统中的位置

```text
Orchestrator._begin_repair_trace
        │
        ▼
RepairRunTracer.begin  ── push root span ── repair_started
        │
        ├─ L2AskMixin agent_ask_started  ── push phase span
        │       │
        │       └─ AgentLoop._emit (tool/model/…) ── 继承当前 span
        │
        ├─ L2AskMixin agent_ask_finished ── emit 后 pop phase
        │
        └─ finalize / cancel ── 闭合悬挂 span ── repair_finished/cancelled
                │
                ▼
        RunStore.append_trace_event ── Canonical 信封 enrich
                │
                ▼
        .agent/runs/<run_id>/trace.jsonl
```

---

## 2. 信封字段（schema_version=1）

| 字段 | 说明 |
|------|------|
| `schema_version` | `"1"` |
| `run_id` / `trace_id` | v1 二者相同 |
| `span_id` / `parent_span_id` | 当前 Span；root 的 parent 为 `null` |
| `event` / `event_type` | 兼容旧键；二者同值 |
| `created_at` / `timestamp` | ISO-UTC；二者同值 |
| `status` | `ok` \| `error` \| `cancelled` \| `unset` |
| `seq` | 每 run 单调递增 |
| `payload` | 可选；经 `redact_artifact` |

---

## 3. Span 规则

1. `repair_started` 前 `TraceSpanContext.reset()` 并 `push("repair_root")`  
2. `agent_ask_started` → `push("ask:<agent>:<phase>")`  
3. `agent_ask_finished` → 先写事件（仍属 ask span），再 `pop`  
4. `finalize` / `cancel`：若仍有 ask 子 span，先写 `span_closed`（`reason=abnormal`）再结束  
5. 结束后 `reset()` 清空 ContextVar，防止泄漏  

---

## 4. 读取与还原

```python
from agent_runtime.run_store import RunStore
from agent_runtime.canonical_trace import validate_event, order_events

store = RunStore(repo_root)
events = store.load_trace_events(run_id)
ordered = store.load_ordered_trace(run_id)  # timestamp + seq
for ev in ordered:
    assert not validate_event(ev)
```

示例样例：[docs/examples/canonical-trace-sample.jsonl](examples/canonical-trace-sample.jsonl)

---

## 5. 一句话总结

Canonical Trace 在既有 JSONL 上叠加统一信封与 Span 栈，使 Issue→Verifier 主链路事件可校验、可排序、可构树，并为后续观测导出留出稳定接口。
