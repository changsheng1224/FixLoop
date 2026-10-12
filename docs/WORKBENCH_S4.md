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
python -m src.cli workbench --provider anthropic_compat --repair-tier host
# 等价入口：python -m src.workbench
```

认证、Provider 配置和 SSE 重连方式沿用 S2。三个模式共用 `--provider`、`--model`、
`--base-url`、`--timeout` 和 `--max-output-tokens`。
`--repair-tier host` 使用本地验证；`container` 要求现有容器验证能力，不可用时失败。
该选项控制 Layer 2 验证层，Patcher 沿用现有工具执行策略。S4 未接入 WSL/bwrap 执行后端。
`fake` 可演示普通聊天，但固定文本不能证明修复成功。

每轮使用新客户端和运行时，仅注入当前节点的祖先消息及新请求。项目模式将消息以 JSON
作为一次新请求传入，不恢复旧 session、工具证据或计划。运行状态保存于
`<state-dir>/runtime/<run-id>`，包括 session、缓存、记忆、修复日志及先例。
不同轮次不复用这些状态；工作台修复固定在注册的项目根执行，不另建 L2 worktree，
也不扩展到上层 Git 仓库根目录。

## 文件与会话树

每轮接纳前保存实际输入快照，若与分支头不同，先在原分支追加“本地修改”检查点。
执行结束后保存实际输出快照，并与 Run 终态在同一历史事务中提交。
成功、失败、停止和中断后的部分文件改动都属于该轮输出；`artifacts.changed_files`
列出输入、输出之间新增、删除或变化的管理路径。它反映实际文件差异，不区分模型与并发外部编辑。
项目会话中的 `chat` 同样记录快照，但模型没有项目工具。

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

停止与超时采用协作式取消；超时项目 Run 记录 `cancelled`、error 为 `timeout`，
服务关闭记录 `interrupted`。取消后先等待仍可能写文件的嵌套工作退出，再保存最终快照。
该等待可能超过 `--timeout`；期间不能恢复或继续同一会话，关闭服务等待超时也保留所有权锁。
项目执行期间仍可读取会话树、Run 和事件，浏览位置与重命名等项目操作需等待执行结束。

重启会保存遗留 queued/running 项目的实际文件状态并标记 `interrupted`，不重放模型或工具。
若终态快照因磁盘故障、文件范围超限或外部持续写入而无法保存，Run 保持非终态，
继续执行和历史恢复均被阻止。处理文件或存储问题后重启服务，可再次捕获当前实际状态；
目前没有在线补交快照接口。原快照和检查点仍保留。

恢复中断的 complete/rollback、受保护文件范围、容量限制及并发外部编辑处理沿用 S3。
文件快照不覆盖数据库、远端请求或其他项目外副作用，也不锁住外部编辑器。
MVP 仍为一个项目一个会话，未提供快照垃圾回收或跨会话项目切换；本地 Web UI 见 S5。

## 验证

`tests/test_workbench_project.py` 覆盖历史输入恢复、原未来与手动修改保留、只读工具范围、
新运行时、失败/取消部分快照、迟到写入等待、重启和快照失败阻断。
实际 Layer 1 测试通过 fake 模型执行读取工具；实际 Layer 2 测试通过 fake 模型生成磁盘补丁，
并在小型项目中运行真实本地 pytest 验证。HTTP 使用真实 loopback 服务验证项目重执行流程。
修复装配、先例和记忆等模块另有相关回归。

验证使用同一个 Windows Python 解释器，按 CLAUDE.md 仅执行相关测试与 Ruff，
失败修复后仅复验失败或直接受影响的用例。未运行仓库全量测试、真实远端模型、
容器端到端验证或 WSL 服务测试。
