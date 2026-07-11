# trace.jsonl gzip 归档设计

> **Bonus ref:** docs/bonus.md §19.1 — trace.jsonl gzip
> **Layer:** L1
> **Status:** in progress

## FixLoop Context

- **Bonus ref:** docs/bonus.md §19.1
- **Layer:** L1 — `agent_runtime/run_store.py`
- **Primary modules:** `run_store.py`, `agent_loop.py`, `replay.py`
- **Acceptance:** `pytest tests/test_persistence.py tests/test_providers_replay.py -v`
- **Branch:** V1.2-Bonus8-observability

## 1. 设计

### 1.1 触发时机

`run_finished` 后，在 `_finalize_run` 末尾调用 `store.compress_trace_if_needed(run_id)`。

### 1.2 阈值

`FIXLOOP_TRACE_GZIP_LINES` 环境变量，默认 1000。测试 trace 通常 <50 行，不受影响。

### 1.3 行为

```
trace.jsonl > N 行 → gzip → trace.jsonl.gz + rm trace.jsonl
                  → report.json 记录 compressed: true + original_bytes + compressed_bytes
```

### 1.4 透明读取

`RunStore.read_trace_lines(run_id) -> list[str]`：
- 优先读 `trace.jsonl`
- 不存在则读 `trace.jsonl.gz`（gzip.open 解压）
- 都不存在返回空列表

## 2. 验收

- [ ] trace < 1000 行不触发压缩
- [ ] trace > 1000 行 → .gz 生成 + 原文件删除
- [ ] read_trace_lines 透明读取 .jsonl 和 .gz
- [ ] replay.py 通过 helper 读取
- [ ] 环境变量 FIXLOOP_TRACE_GZIP_LINES 可配
