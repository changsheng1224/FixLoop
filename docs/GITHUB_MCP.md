# GitHub MCP 最小闭环（实现链路）

> 抽象说明 FixLoop 如何对接 GitHub MCP：从 Client / Transport，到 Registry、权限与 Observation。  
> **代码权威目录**：`agent_runtime/mcp/`。计划勾选见 `docs/2026-08-03-to-08-09-enhancement-plan.md`（8 月 5 日｜GitHub MCP）。

---

## 1. 问题与边界

### 1.1 要解决什么

修复 Agent 需要读 GitHub 上的 Issue / PR / Commit / Actions 上下文，并在人工确认后创建 **Draft PR**。系统希望：

1. 用标准 MCP 协议子集（`tools/list`、`tools/call`）对接外部 GitHub MCP Server  
2. 把远程工具**适配成** FixLoop 本地工具表条目（稳定 `github_*` 名）  
3. 经既有 **ToolGateway + ToolExecutor Gate7** 做角色权限与写操作审批  
4. 结果统一为 Observation 字符串；经 Executor 时复用既有 `tool_executed` Trace  

### 1.2 明确不做

- 不引入 LangChain / 官方 MCP Python SDK 作为运行时依赖  
- 首期不做 OAuth 浏览器流、不做 `api.githubcopilot.com` 远程 HTTP transport  
- 不做 Canonical Trace 专用 MCP 事件信封（那是另一条主线）  
- 不把 MCP 工具默认并入 repair **canonical** 全集（避免破坏 schema-sync）  
- 不开放 Merge / 删分支 / Secrets / 仓库管理类写操作  

### 1.3 在系统中的位置

```text
Repair Agent（localizer / retriever / patcher …）
        │
        ▼  FIXLOOP_ENABLE_GITHUB_MCP=1
┌───────────────────────────────┐
│ build_repair_agent_tools      │  canonical ∪ GitHub MCP tools
└───────────────┬───────────────┘
                │
                ▼
┌───────────────────────────────┐
│ ToolGateway（角色权限）        │  REPAIR_PERMISSION_TABLE
└───────────────┬───────────────┘
                │
                ▼
┌───────────────────────────────┐
│ ToolExecutor Gate7            │  读=auto / Draft PR=ask / 危险=deny
└───────────────┬───────────────┘
                │
                ▼
┌───────────────────────────────┐
│ MCP Client + Transport        │  Mock 进程内 或 官方 stdio
│ → Observation 字符串          │
└───────────────────────────────┘
```

---

## 2. 能力全景

| 能力 | 说明 |
|------|------|
| MCP Client | `tools/list` / `tools/call`；超时、不可用、Schema 错误归一 |
| Transport | `InProcessTransport`（Mock）；`StdioTransport`（官方 NDJSON JSON-RPC） |
| Mock Server | 本地假 GitHub 工具集，含危险工具供拒绝测试 |
| 官方映射 | 本地 `github_*` ↔ 官方工具名/参数（`official_map.py`） |
| Allowlist | 只读集 + 唯一写 `github_create_draft_pr`；显式 deny 危险名 |
| Registry Adapter | list → 过滤 → FixLoop `{schema,run,risky,…}` |
| 权限接线 | Gateway 角色表 + Gate7 ask/auto |
| 自动选型 | 有 PAT 且非强制 mock → 官方；失败回退 Mock |
| Live 测 | `FIXLOOP_GITHUB_MCP_LIVE=1` 真连只读工具 |

---

## 3. 端到端链路（抽象）

```text
开启 FIXLOOP_ENABLE_GITHUB_MCP
            │
            ▼
   open_github_mcp_client / build_github_mcp_tools_auto
            │
     ┌──────┴──────┐
     │             │
  MODE=mock     auto / official
  或无 token      + PAT
     │             │
     ▼             ▼
 MockServer    StdioTransport
 InProcess     → github-mcp-server
     │         (docker 或本地 exe)
     │             │
     │             ▼
     │      OfficialMappedClient
     │      （名/参适配）
     │             │
     └──────┬──────┘
            ▼
 build_github_mcp_tool_registry
   tools/list → allowlist → schema/run
            │
            ▼
 合并进 Agent.tools（非 canonical）
            │
            ▼
 Agent.execute_tool(name, args)
   → Gateway.can_call(role, name)
   → Executor Gate7（ask/auto）
   → run() → client.call_tool
   → Observation；既有 Trace/callback
```

闭环口诀：

`tools/list → Registry → 权限过滤 → tools/call → 错误归一化 → Observation（+ 既有 Trace）`

---

## 4. 模块与文件

| 路径 | 职责 |
|------|------|
| `agent_runtime/mcp/client.py` | `McpClient`、`InProcessTransport`、结果归一 |
| `agent_runtime/mcp/stdio.py` | stdio JSON-RPC：`initialize` / `tools/*` |
| `agent_runtime/mcp/mock_server.py` | 进程内假 Server |
| `agent_runtime/mcp/official.py` | 官方启动、PAT、`OfficialMappedClient` |
| `agent_runtime/mcp/official_map.py` | 本地↔官方名与参数适配 |
| `agent_runtime/mcp/github_allowlist.py` | 读/写/拒绝名单 |
| `agent_runtime/mcp/schema_map.py` | JSON Schema → FixLoop schema；参数校验 |
| `agent_runtime/mcp/errors.py` | `McpTimeoutError` / `Unavailable` / `Schema` |
| `agent_runtime/mcp/registry.py` | Registry 构建、Mock/官方自动打开 |
| `src/middleware.py` | `REPAIR_PERMISSION_TABLE` 增补 MCP 权限 |
| `agent_runtime/tool_executor.py` | Gate7：读 auto；`github_create_draft_pr` ask |
| `src/tools/composite.py` | env 开关并入 repair 工具表 |

---

## 5. 本地工具契约（稳定面）

Agent / Gateway / 测试只看见下列 **本地名**（不直接暴露官方 `list_issues` 等）：

### 5.1 只读（Gate7 = auto）

| 本地名 | 官方远程（映射后） | 备注 |
|--------|-------------------|------|
| `github_list_issues` | `list_issues` | |
| `github_get_issue` | `issue_read` | `method=get`，`number→issue_number` |
| `github_list_issue_comments` | `issue_read` | `method=get_comments` |
| `github_get_repo` | `get_file_contents` | 根路径 listing（官方无独立 get_repo） |
| `github_list_commits` | `list_commits` | |
| `github_get_commit` | `get_commit` | |
| `github_list_branches` | `list_branches` | |
| `github_list_pull_requests` | `list_pull_requests` | |
| `github_get_pull_request` | `pull_request_read` | `method=get`，`number→pullNumber` |
| `github_list_workflow_runs` | `actions_list` | `method=list_workflow_runs` |

### 5.2 唯一写（Gate7 = ask；Gateway 仅 patcher）

| 本地名 | 官方远程 | 约束 |
|--------|----------|------|
| `github_create_draft_pr` | `create_pull_request` | **强制 `draft=true`** |

### 5.3 拒绝注册（即使 Server list 出来）

`merge_pull_request`、删分支、改 Secrets、以及 allowlist 中的其它危险名；Mock 故意暴露若干项供单测断言「未进入 Registry」。

### 5.4 角色权限（摘要）

| 工具类 | localizer | retriever | patcher |
|--------|-----------|-----------|---------|
| 只读 `github_*` | ✓ | ✓ | ✓ |
| `github_create_draft_pr` | ✗ | ✗ | ✓（且须 ask） |

---

## 6. 环境变量与运行方式

| 变量 | 作用 |
|------|------|
| `FIXLOOP_ENABLE_GITHUB_MCP` | `1/true/yes` 时并入 repair Agent 工具表 |
| `FIXLOOP_GITHUB_MCP_MODE` | `mock` 强制 Mock；`official`/`real`/`stdio` 强制官方；空=auto |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | 官方认证（亦认 `GITHUB_PAT` / `FIXLOOP_GITHUB_TOKEN`） |
| `FIXLOOP_GITHUB_MCP_COMMAND` | 覆盖默认启动命令（如本地 `github-mcp-server.exe stdio`） |
| `FIXLOOP_GITHUB_MCP_IMAGE` | Docker 镜像（默认 `ghcr.io/github/github-mcp-server`） |
| `FIXLOOP_GITHUB_MCP_TOOLSETS` | 默认 `repos,issues,pull_requests,actions` |
| `FIXLOOP_GITHUB_MCP_LIVE` | `1` 时跑真连 pytest |
| `FIXLOOP_GITHUB_MCP_OWNER` / `_REPO` | live 测仓库（默认 `changsheng1224/FixLoop`） |

### 6.1 Mock（默认测试 / 无 token）

```text
FIXLOOP_ENABLE_GITHUB_MCP=1
FIXLOOP_GITHUB_MCP_MODE=mock   # 可选但推荐在 CI 明确
```

### 6.2 官方 stdio（本机二进制示例）

```powershell
$env:FIXLOOP_ENABLE_GITHUB_MCP=1
$env:FIXLOOP_GITHUB_MCP_MODE='official'
$env:GITHUB_PERSONAL_ACCESS_TOKEN=(gh auth token)
$env:FIXLOOP_GITHUB_MCP_COMMAND='.\.tmp\github-mcp-server\github-mcp-server.exe stdio'
```

默认无自定义 command 时走：

`docker run -i --rm -e GITHUB_PERSONAL_ACCESS_TOKEN -e GITHUB_TOOLSETS=… ghcr.io/github/github-mcp-server`

官方启动失败时，`build_github_mcp_tools_auto` **回退 Mock**，避免整条修复流水线因 MCP 挂掉。

---

## 7. Observation、错误与 Trace

### 7.1 Observation

- 成功：`tools/call` 的 text content（或 JSON 文本化）  
- 失败：`Error: [<code>] …`，code ∈ `mcp_timeout` / `mcp_unavailable` / `mcp_schema_error`  

Registry 的 `run()` 吞掉 `McpError`，保证 Agent 侧始终拿到字符串 Observation。

### 7.2 Trace（诚实边界）

- **有**：工具经 `Agent.execute_tool` → `ToolExecutor` 时，复用既有 callback / `tool_executed`  
- **无**：MCP 包内**不**单独写 Canonical Trace 信封；无 MCP 专用 `event_type`  
- Draft PR「审批演示」：当前以 Gate7 `ask` 策略下的**单元测试拒绝**为验收，非交互 CLI 录屏  

---

## 8. 测试地图

| 文件 | 覆盖 |
|------|------|
| `tests/test_mcp_github.py` | Allowlist、Mock client、超时/不可用/Schema、Registry、Gateway 越权、Draft PR ask、composite 开关 |
| `tests/test_mcp_official_stdio.py` | 假 stdio 握手、官方名参映射、MODE=mock 强制、`TestLiveOfficialGithubMcp` |
| `tests/fixtures/fake_github_mcp_server.py` | 假官方 NDJSON server |

真连（需 PAT + 二进制/Docker）：

```powershell
$env:FIXLOOP_GITHUB_MCP_LIVE=1
$env:GITHUB_PERSONAL_ACCESS_TOKEN=(gh auth token)
$env:FIXLOOP_GITHUB_MCP_COMMAND='…\github-mcp-server.exe stdio'
pytest tests/test_mcp_official_stdio.py::TestLiveOfficialGithubMcp -v
```

Live 覆盖只读：`list_issues` / `get_repo` / `list_branches` / `list_commits` / `get_commit` / `list_pull_requests` / `get_pull_request` / `list_workflow_runs` 等；**不**自动创建真实 Draft PR。

---

## 9. 设计原则（可对外口述）

1. **协议子集 + 可替换 Transport**——先 Mock 闭环，再换 stdio，不改 Registry 契约。  
2. **本地稳定名、远程可演进**——Agent 只见 `github_*`；官方改名由映射层吸收。  
3. **最小写面 + 双层闸**——唯一 Draft PR；Gateway 角色隔离 + Gate7 人工 ask。  
4. **失败可降级**——官方起不来回退 Mock；错误归一为 Observation，不炸环。  
5. **Opt-in**——默认不污染 canonical 工具集。  

---

## 10. 演进方向

1. Streamable HTTP 对接远程 GitHub MCP（Copilot hosted）  
2. OAuth / 设备码流（无 PAT 场景）  
3. MCP 调用写入 Canonical Trace 专用字段（与 Trace 主线对齐）  
4. Draft PR 交互确认演示脚本（REPL / CLI）  
5. 按仓库上下文自动填 `owner/repo` 槽位  

---

## 11. 一句话总结

GitHub MCP 最小闭环把官方（或 Mock）Server 的 `tools/list|call` **适配成** FixLoop `github_*` 工具，经 Allowlist、Gateway 与 Gate7 后再执行；只读自动、Draft PR 必问、危险操作永不注册，Observation 统一字符串并复用既有工具 Trace。
