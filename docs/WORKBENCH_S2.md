# 工作台 S2：本地服务与普通聊天

S2 在 [S1 会话树](WORKBENCH_S1.md) 上提供可启动的本地 JSON/SSE 服务，覆盖开发讨论、
写作和日常问答。普通聊天不绑定项目，不构造 Agent、不注册工具、不读取项目源码。
历史编辑和重新生成创建新分支，保留原分支及其后续轮次。

本阶段交付后端和命令行入口。项目文件快照、手动修改保护及回退见 [S3](WORKBENCH_S3.md)；
项目问答和修复执行接入属于 S4；桌面聊天界面属于 S5。

## 启动

使用与安装依赖相同的 Python：

```powershell
python -m pip install -e ".[dev]"
python -m src.cli workbench --provider anthropic_compat
# 安装后的等价入口：fixloop workbench --provider anthropic_compat
# 独立入口：python -m src.workbench --provider anthropic_compat
```

无需外部模型的本地演示：

```powershell
python -m src.cli workbench --provider fake --port 8765 --state-dir .runtime-tools/workbench-demo
```

`fake` 返回固定演示文本，用于验证链路。真实模型采用下列配置；启动参数 `--model`
和 `--base-url` 优先于环境变量。沿用 Layer 1 的 bootstrap，支持进程环境和当前目录
`.env`（不覆盖已有环境变量）。

| Provider | 协议 | 环境变量 | 默认值 |
| --- | --- | --- | --- |
| `anthropic_compat` | `/messages` SSE | `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL` | DeepSeek Anthropic 兼容接口，`deepseek-v4-pro` |
| `openai` | `/responses` SSE | `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL` | `https://api.openai.com/v1`，`gpt-4o` |
| `ollama` | `/api/chat` NDJSON | `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | `http://127.0.0.1:11434`，`qwen3.5:9b` |
| `fake` | 本地固定回答 | 无 | 无网络请求 |

服务只监听 `127.0.0.1`。默认端口 8765；`--port 0` 由系统分配，启动输出显示实际地址。
默认状态目录是 `~/.fixloop/workbench`；可用 `--state-dir` 指定。每次启动生成独立 API
Bearer token，写入状态目录 `api-token`，正常退出时删除。Token 不放在 URL 中，
也不使用模型 API key 作为本地服务凭据。

状态目录包含 `workbench.sqlite3`（S1 会话树）、`events.sqlite3`（S2 事件日志）、
`service.lock`（进程所有权锁）及运行期间的 token 文件。聊天文本保存在本地 SQLite 中。
同一状态目录只能由一个服务持有；进程崩溃后操作系统自动释放锁，无需手动删锁文件。

## API

所有请求均需 `Authorization: Bearer <api-token>`，包括健康检查。
`Host` 必须是服务实际端口的 `127.0.0.1` 或 `localhost`；存在 `Origin` 时必须匹配这两个
本地 HTTP origin 之一。POST/PATCH 使用 `application/json`、单个 `Content-Length`，
请求体上限 1 MB，不支持 chunked 请求体。

下表中 `C=/api/conversations/{conversation_id}`。标有 `expected_revision?` 的写操作可携带
当前会话 revision；陈旧请求返回 409，且不改变历史。revision 不只随发送递增，
浏览、重命名和执行终态写入也会更新，调用方应重新读取当前会话。

| 方法与路径 | JSON 请求 / 查询 | 返回 |
| --- | --- | --- |
| `GET /api/health` | 无 | 200：阶段和支持模式 |
| `GET /api/conversations` | 无 | 200：`{conversations: [...]}` |
| `POST /api/conversations` | `{title?}` | 201：Conversation |
| `GET C` | 无 | 200：Conversation |
| `PATCH C` | `{title, expected_revision?}` | 200：Conversation |
| `GET C/tree` | 无 | 200：ConversationTree，包括分支、节点和 Run |
| `GET C/messages` | `?node_id=...`，省略为当前执行分支末端，空值为根 | 200：`{messages: [...]}` |
| `POST C/turns` | `{text, mode?: "chat", expected_revision?}` | 202：`{node, run}` |
| `POST C/nodes/{node_id}/edit` | `{text, expected_revision?}` | 202：新分支的 `{node, run}` |
| `POST C/nodes/{node_id}/regenerate` | `{expected_revision?}` | 202：新分支的 `{node, run}` |
| `POST C/view` | `{node_id: id或null, expected_revision?}` | 200：Conversation |
| `POST C/fork` | `{node_id: id或null, name?, expected_revision?}` | 201：Branch |
| `POST C/switch` | `{branch_id, expected_revision?}` | 200：Branch |
| `GET C/runs/{run_id}` | 无 | 200：`{run, partial_answer, event_cursor}` |
| `POST C/runs/{run_id}/stop` | `{}` | 202：Run（停止请求已接受，可能尚未终止） |
| `GET C/runs/{run_id}/events` | `?after=N` 或 `Last-Event-ID` | 200：SSE |

POST/PATCH 拒绝未知字段。节点、分支和 Run 必须属于路径中的会话。
`project_path` 非空或执行模式为 `project_chat`/`repair` 时返回 422；
对已有项目会话的执行、分叉及分支切换也在修改历史前拒绝。

主要错误：400 参数错误，401 无效凭据，403 无效 Host/Origin，404 路由或对象不存在，
409 陈旧 revision、会话忙碌或执行容量已满，422 本阶段不支持的模式，429 SSE 连接已满，
500 持久化等内部错误。Provider 调用在后台运行，失败通过 Run 和事件呈现，
不改变发送请求已经返回的 202 状态。

### PowerShell 示例

在另一个终端中使用演示服务：

```powershell
$workbenchUrl = 'http://127.0.0.1:8765'
$workbenchToken = (Get-Content -Raw .runtime-tools/workbench-demo/api-token).Trim()
$workbenchHeaders = @{ Authorization = "Bearer $workbenchToken" }
$chat = Invoke-RestMethod "$workbenchUrl/api/conversations" -Method Post `
    -Headers $workbenchHeaders -ContentType 'application/json' `
    -Body '{"title":"Writing"}'
$turn = Invoke-RestMethod "$workbenchUrl/api/conversations/$($chat.id)/turns" -Method Post `
    -Headers $workbenchHeaders -ContentType 'application/json' `
    -Body '{"text":"Help me draft a short note."}'
$runUrl = "$workbenchUrl/api/conversations/$($chat.id)/runs/$($turn.run.id)"
Invoke-RestMethod $runUrl -Headers $workbenchHeaders
curl.exe -N -H "Authorization: Bearer $workbenchToken" "$runUrl/events"
```

### SSE 与重连

事件顺序是 `run.queued` → `run.started` → 多个 `answer.delta` → `run.finished`。
停止时还会产生 `run.stop_requested`。每个事件先持久化再传给客户端：

```text
id: run-<id>:3
event: answer.delta
data: {"sequence":3,"run_id":"run-<id>","kind":"answer.delta","data":{"text":"你好"}}

```

序号按 Run 独立递增，从 1 开始。重连使用 `Last-Event-ID: RUN_ID:SEQUENCE`，
或 `?after=SEQUENCE`，从下一个事件继续；两者同时提供时 header 优先。
游标必须属于当前 Run 且不超过已有事件序号，0 表示从头读取。
事件中的换行由 JSON 转义；空闲连接每五秒发送 SSE 注释心跳。

终态时服务还会发送 `event: run.snapshot`，data 为完整 Run，没有 replay ID，随后关闭流。
这个权威快照处理终态写入与事件日志写入之间的间隙，也让已经消费全部事件的重连客户端
确认终态。客户端应按 event 区分封装事件与 Run 快照，以 snapshot/Run 为最终状态。
浏览器端应使用带 Authorization header 的流式 fetch；原生 EventSource 不能直接设置该 header。
关闭浏览页或断开 SSE 只分离观察者，后台执行继续。

## 历史与执行语义

每次执行使用新 ModelClient，只发送选中节点的祖先路径及当前用户消息，保留真实
`user`/`assistant` 角色。不共享 Agent memory、工具证据或其他会话上下文。
已结束且包含回答的 Run 会进入聊天上下文，包括失败/停止时保存的部分回答；
运行中的回答通过 SSE/Run 查询显示，不提前投影为已结束消息。

例如原历史为 `1 → 2 → 3`，编辑第 1 轮并执行生成新分支 `1'`；原来的 `1 → 2 → 3`
仍完整存在。新回答不消费原第 1 轮的回答或后续第 2、3 轮，也不自动重新执行它们。
之后发送第 2' 轮沿新分支继续；切回原分支时从原第 3 轮之后继续。
重新生成同样创建同级新节点，不覆盖旧回答。

`view` 只改变浏览节点，发送仍从执行分支末端继续；`fork` 从指定节点创建并选择新分支，
`switch` 选择已有分支。`node_id: null` 表示根。会话有 queued/running Run 时，
追加、编辑、重跑、分叉和切换均返回 409；读取、浏览和重命名仍可使用。

普通聊天不修改项目文件，因此这些分支操作不涉及代码回退。
项目会话的快照恢复和用户手动修改保护必须由 S3 协调，不能直接调用 S1 切换分支来回退文件。

## 停止、失败与重启

Run 生命周期：`queued → running → succeeded/failed/cancelled/interrupted`。
queued 也可直接结束。仅完整、非空的模型文本才成功；截断、过滤、工具调用、协议错误、
输出限额超出和超时均不能作为成功回答。模型推理文本不作为 Anthropic 回答 delta 输出。
不会在输出后自动重试模型请求。

用户 stop 保留已持久化的部分回答并结束为 cancelled；正常关闭服务则结束为 interrupted。
关闭服务对 worker 的等待有界；无法退出的自定义 executor 会保留所有权锁并报告超时。
默认 ChatExecutor 使用独立 daemon 传输线程，即便网络读取阻塞也能及时停止逻辑执行。
取消 token 和执行闸门阻止后续 delta 写入已经结束的 Run。

逻辑停止不保证远端模型立即停止计算或计费。尚未返回的传输占据容量，直到它真正退出，
避免重复停止制造无限量网络请求。服务重启在拿到独占锁后，把遗留 queued/running Run
标记 interrupted，并从事件日志恢复部分回答；不会重放模型请求或工具。
终态 Run 缺失的 `run.finished` 事件会在启动时补齐。

Run 的 error 只持久化 Provider 错误类别或异常类型，避免传输异常文本泄露凭据；
成功 Run 的 artifacts 保存归一化的 usage 和 finish。

默认限制：8 个活跃逻辑 Run、4 个活跃模型传输、16 条 SSE 连接、32 条 HTTP 请求连接。
回答最多 1 MB，上下文消息内容总计最多 400 KB；过长历史应在较早节点分叉或另开会话。
模型输出默认 4096 token，可用 `--max-output-tokens` 调整到 1–8192；
`--timeout` 默认 120 秒，包含等待模型容量的时间。

## 模块与验证

| 模块 | 职责 |
| --- | --- |
| `agent_runtime/providers/chat.py` | Layer 1 的结构化无工具流式模型调用 |
| `src/workbench/chat.py` | 新客户端、上下文/输出预算、取消、期限和传输容量 |
| `src/workbench/service.py` | 会话操作、调度、终态保存和恢复 |
| `src/workbench/events.py` | 独立 SQLite 事件日志与重连回放 |
| `src/workbench/ownership.py` | Windows/Linux 服务所有权锁 |
| `src/workbench/http.py` | 本地鉴权、JSON API 和 SSE |
| `src/workbench/__main__.py` | 启动配置、token 和服务生命周期 |

相关测试使用本地 mock Provider 和实际 loopback HTTP，无需外部模型凭据：

```powershell
python -m pytest tests/test_workbench_service.py tests/test_workbench_http.py tests/test_chat_provider.py -q --basetemp=.runtime-tools/pytest-workbench-s2
python -m ruff check agent_runtime/providers/chat.py src/workbench src/cli.py tests/test_workbench_service.py tests/test_workbench_http.py tests/test_chat_provider.py
python -m ruff format --check agent_runtime/providers/chat.py src/workbench src/cli.py tests/test_workbench_service.py tests/test_workbench_http.py tests/test_chat_provider.py
```

覆盖历史树、上下文隔离、并发发送、持久化回放、真实 SSE 先于完成交付、断连、停止、
阻塞传输隔离、独占锁、子进程崩溃恢复、CLI 子进程启动、鉴权与 Provider wire contract。
S1 存储和 bootstrap 另有相关回归测试；本阶段未运行全量测试，也未调用真实外部模型。
