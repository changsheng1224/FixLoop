# 工作台 S4：项目问答与修复执行

S4 将 [S2 本地服务](WORKBENCH_S2.md) 和 [S3 文件快照](WORKBENCH_S3.md) 接入真实
Layer 1 问答与 Layer 2 修复。普通聊天、项目问答、修复共用会话树、JSON/SSE API，
历史编辑或重新生成保留原分支及后续轮次。浏览器聊天和会话树界面已由 [S5](WORKBENCH_S5.md) 提供。

## 模式与启动

| mode | 用途 | 执行方式 |
| --- | --- | --- |
| `chat` | 开发讨论、写作、日常问答 | 无工具模型调用，可用于独立或项目会话 |
| `project_chat` | 阅读项目、解释代码、定位讨论 | 新 Layer 1 Agent，仅注册 `list_files`、`read_file`、`grep` |
| `repair` | 修改项目并验证 | 新 Layer 2 orchestrator，复用现有 Patcher 和验证流程 |

项目模式必须绑定 `project_path`。项目问答使用 S3 固定文件范围，拒绝受保护路径及越界访问；
不提供 shell 或写工具。读取限 UTF-8，每次最多 1,000 行、60,000 字符，检索是字面子串匹配，
最多 1,000 条结果。修复工具沿用 Layer 2 策略，快照范围仍由 S3 定义。

```powershell
python -m src.cli workbench --provider anthropic_compat --repair-tier sandbox
# 等价入口：python -m src.workbench
```

认证、Provider 配置和 SSE 重连方式沿用 S2。三个模式共用 `--provider`、`--model`、
`--base-url`、`--timeout` 和 `--max-output-tokens`。
`--repair-tier sandbox` 是唯一生产修复模式，使用已有 Windows WSL/bwrap 受约束后端；
环境不可用时失败，不退回 host。`fake` 可演示普通聊天，但不能证明修复成功。

每轮使用受监督的独立进程和新运行时。上下文由当前祖先路径构建；长历史使用带内容摘要、
模型与项目快照版本的增量摘要缓存，原始消息始终保留。不会继承旧 session、工具证据或计划。
运行状态保存于 `<state-dir>/runtime/<run-id>`，项目输入物化到独立
`<state-dir>/workspaces/<run-id>/tree`。注册目录的手动修改也包含在输入中；不另建 L2 Git
worktree，也不把执行根扩展到上层仓库。详见 [加固契约](WORKBENCH_HARDENING.md)。

## 文件与会话树

每轮接纳前保存实际输入快照，若与分支头不同，先保存“本地修改”检查点。
独立工作区的输出快照与 Run 终态、终态事件在同一 SQLite 事务提交。
成功修复生成候选结果，先查看差异，再明确应用到注册项目；失败、停止和中断产生的部分修改
仅保留为隔离证据。`artifacts.changed_files` 表示隔离输入与输出的差异。
应用前核对项目版本、根身份与实时文件；外部编辑或另一个候选已应用时拒绝覆盖。
同一项目可绑定多个会话，执行工作区独立，原目录写入串行。跨服务项目所有权锁保留。

例如 `1 → 2 → 3` 中第 3 轮修改代码，随后用户又手动修改文件。编辑第 1 轮时：

1. 为第 1 轮生成 `input` 恢复预览，目标是该轮执行前的快照。
2. 将预览 ID 与修改后的文本一起提交。服务再次核对 revision、实时文件和目标节点。
3. 保存当前手动修改到原分支，恢复第 1 轮输入，在新分支执行修改后的第 1 轮。
4. 原来的 1、2、3 和手动修改仍在原分支；新分支后续沿新结果继续。
5. 新执行不继承或自动重放原第 2、3 轮。切回原分支时，使用 S3 的 `switch` 预览与应用。

恢复通过快照计算文件变化，不依赖反向补丁。若恢复完成后新轮次接纳或调度失败，
已保存的恢复检查点仍然有效；读取会话树确认状态后，可在该新分支发送请求。

## API

下表中 `C=/api/conversations/{conversation_id}`。所有请求沿用 S2 Bearer 认证。

| 方法与路径 | JSON 请求 | 结果 |
| --- | --- | --- |
| `GET /api/health` | 无 | `stage: "S5"`，三个支持模式 |
| `POST /api/conversations` | `{title?, project_path}` | 201，登记项目基线 |
| `POST C/turns` | `{text, mode?, expected_revision?}` | 202，已接纳的新轮次 |
| `POST C/restores/preview` | `{action: "input", node_id, expected_revision?}` | 201，文件恢复预览 |
| `POST C/nodes/{node_id}/edit` | `{text, plan_id, expected_revision?}` | 202，恢复后新分支执行 |
| `POST C/nodes/{node_id}/regenerate` | `{plan_id, expected_revision?}` | 202，恢复后重新执行原请求 |
| `GET C/runs/{run_id}` | 无 | Run、部分回答和事件游标 |
| `POST C/runs/{run_id}/stop` | `{}` | 202，接受停止请求 |

`mode` 默认 `chat`；编辑和重新生成沿用原节点模式。独立聊天的这两个操作无需 `plan_id`。
项目重执行必须使用该节点专属的 `input` 预览，不能使用其他节点、`fork` 或 `switch` 预览。
客户端展示预览后，直接调用 edit/regenerate，**不要先单独调用 restores/apply**。
预览过期、revision 变化或外部编辑导致预览失效时返回 409，需重新预览。

示例请求体（顺序调用，替换返回的 ID）：

```json
{"title":"项目讨论","project_path":"C:/work/example"}
```

```json
{"text":"解释入口如何加载配置","mode":"project_chat"}
```

```json
{"text":"修复配置为空时的异常，并验证","mode":"repair"}
```

```json
{"action":"input","node_id":"node-<历史轮次>"}
```

```json
{"text":"改为处理配置缺失与空配置两种情况","plan_id":"<预览返回的id>"}
```

SSE 在 S2 事件之外增加 `project.progress`（阶段、步骤或工具名）和 `project.report`
（修复状态、验证摘要）。项目模式当前在完整答案产生后发送 `answer.delta`；
过程进度可提前到达，尚不提供逐 token 回答流。

修复只有在 L2 状态为 `fixed` 且验证 `all_passed` 时才成功。Run artifacts 包含
`repair_status`、`verified`、`verification`（测试计数等）；模型声称已修复不能替代验证结果。
终态以 Run 或 SSE 的 `run.snapshot` 为准。

## 停止、故障和边界

停止与超时先发取消信号，经过短宽限期后终止受监督的进程范围。
Windows 使用 Job Object；Linux 普通会话要求已委派的 cgroup v2（修复仍要求 Windows WSL 后端）。
停止后必须确认进程及 WSL 工具资源已退出，才能捕获输出。超时为 `cancelled/timeout`，
服务退出或重启遗留运行为 `interrupted`；资源状态无法确认时整个项目阻止新任务和文件应用，
页面提供重新检查入口。终止本地客户端不代表供应商已经停止远端计算。

服务锁只证明旧控制器退出；重启还会核对持久化 worker 身份、进程范围与沙箱收据。
不会在资源未确认停止时扫描或恢复文件。终态快照提交失败时保留未完成 Run 并阻止继续。
多文件恢复仍非物理原子操作：持久化步骤日志和项目级门禁防止服务误接纳混合状态。
完整目标已落盘且没有既定恢复决定时可自动补交元数据；部分恢复需明确 complete/rollback，
已持久化的决定必须继续，未知外部编辑拒绝覆盖。

文件快照不覆盖远端请求、数据库服务和其他项目外副作用，也不锁住外部编辑器。
尚未提供快照垃圾回收、候选自动合并或多个服务共同管理同一项目。本地 Web UI 见 S5。

## 验证

`tests/test_workbench_project.py` 覆盖历史输入恢复、原未来与手动修改保留、只读工具范围、
新运行时、失败/取消部分快照、迟到写入等待、重启和快照失败阻断。
实际 Layer 1 测试通过 fake 模型执行读取工具；实际 Layer 2 测试通过 fake 模型生成磁盘补丁，
并在小型项目中运行真实本地 pytest 验证。HTTP 使用真实 loopback 服务验证项目重执行流程。
修复装配、先例和记忆等模块另有相关回归。

验证使用同一个 Windows Python 解释器，按 CLAUDE.md 仅执行相关测试与 Ruff，
失败修复后仅复验失败或直接受影响的用例。未运行仓库全量测试、真实远端模型、
容器端到端验证或 WSL 服务测试。
