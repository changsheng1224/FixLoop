# 附录 A：Web 产品化（Out of Scope）

> 🚫 **FixLoop 主线不实现**本节全部条目。产品定位为 **本地 CLI / REPL + repair 流水线**；下列内容仅作 **设计归档**，供 fork 为 SaaS 或面试讨论「若做 Web 如何不 bypass ToolGateway」时参考。  
> **现状**：代码库 **无** Web UI · **无** REST/SSE 服务 · **无** `--serve` · **无** 认证/多租户。

### A.1 前端（原 §23.1）

- 🚫 **[P1] 实时进度页**：SSE 订阅 localize → retrieve → patch → verify
- 🚫 **[P1] 结果页**：patch diff 高亮、verify 报告、下载 `.patch`

### A.2 HTTP API（原 §23.2）

- 🚫 **[P1] REST v1 契约**：`POST /api/v1/repairs` · `GET .../{id}` · `POST .../cancel`
- 🚫 **[P1] SSE**：`GET /api/v1/repairs/{id}/events`
- 🚫 **[P2] Idempotency-Key** · **WebSocket** · **`/health` + Redis ready**

### A.3 认证 · 配额 · 隔离（原 §23.3）

- 🚫 JWT/session · `tenant_id`/`user_id` · API Key · RPS 配额 · workspace jail · 同 repo 写锁（Web 语义）· Docker 槽位 per 租户

### A.4 Worker · 部署（原 §23.4–23.5）

- 🚫 Redis/RQ 队列 · K8s HPA · NFS/S3 trace · CSRF/CSP

### A.5 已移出 §21 的 API 条目

- 🚫 **REST API**：~~`--serve :8000`，POST `/ask` + GET `/session/{id}`~~ → 本地用 REPL / repair CLI 替代

### A.6 本地等价能力（见 [bonus.md](../bonus.md) 待办）

| Web 设想 | 本地替代 |
|----------|----------|
| `POST /cancel` | Ctrl+C · REPL `/cancel`（设计文档 §2.1） |
| 进度 SSE | CLI progress callback · `trace.jsonl` · `--verbose` |
| 多 repair 并发 | 文件锁 + temp workspace（设计文档 §12.6） |
| 断点续跑 | `--resume-repair`（设计文档 §11） |

---

*筛选版 · 本地运行 · base `master` @ PR #87 · 558 tests · 80% coverage*
