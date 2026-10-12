# 工作台 S6：端到端验证

S6 验证 S1–S5 的完整工作台链路，新增的执行验收使用生产 ChatExecutor、ProjectExecutor、
模型客户端、L1 工具、L2 编排、本地 pytest、文件快照和 HTTP 服务。
本地测试服务按脚本返回 Ollama 协议响应，避免依赖远端模型、凭据和随机输出。
脚本只决定测试模型的回答，文件修改与验证仍由生产工具和执行器完成。

## 验收入口

在安装了项目与开发依赖的同一个 Python 环境中执行：

```powershell
python tests/run_workbench_e2e.py --group api
# 默认运行 api、browser、units 三组：
python tests/run_workbench_e2e.py --browser-channel msedge
```

浏览器验收额外需要 Node、Playwright 模块和浏览器。Windows 可直接使用已经安装的 Edge。
测试依赖可装在忽略的工具目录中，工作台运行本身仍不需要 Node：

```powershell
npm install --prefix .runtime-tools/s6-node --no-save playwright
$env:FIXLOOP_PLAYWRIGHT_MODULE = (Resolve-Path .runtime-tools/s6-node/node_modules/playwright).Path
python tests/run_workbench_e2e.py --browser-channel msedge
```

也可用 `--node <可执行文件>`、`--playwright-module <模块绝对路径>` 指向已有测试环境。
Linux 未指定 channel 时使用 Playwright Chromium，需事先安装对应浏览器。
脚本不自动联网安装依赖，缺少依赖会失败并返回非零状态，不会静默跳过。

| 组 | 内容 |
| --- | --- |
| `api` | 独立服务进程上的 10 个生产执行链路用例，包含参数化故障场景 |
| `browser` | Edge/Chromium 上的普通聊天与项目文件生命周期两组验收 |
| `units` | S5 SSE 任意 UTF-8 字节分块、心跳、CRLF 与祖先路径测试 |
| `regression` | 可选的工作台 service/project/http/restore/ui 相关回归，不包含仓库全量测试 |

部分失败后只复验对应场景：

```powershell
python tests/run_workbench_e2e.py --group api --case 'test_partial_repair_terminal_state_and_restart[stop]'
python tests/run_workbench_e2e.py --group browser --case project_lifecycle --browser-channel msedge
# 可重复 --case 或 --group；--case 要求只选择 api 或 browser 一组。
```

## 覆盖矩阵

| 场景 | 验收证据 |
| --- | --- |
| 普通写作、历史编辑 | 实际 CLI 启动，流式 HTTP 协议；请求保持角色结构、没有工具、没有原分支未来轮次 |
| 项目只读问答 | 实际 read_file 结果进入下一次模型请求；禁止 patch_file 与受保护文件读取 |
| 代码修复 | 初始 pytest 失败，实际 patch_file 修改源码，L2 本地 pytest 通过且 verified=true |
| 多轮修复后编辑早期轮次 | 恢复该轮输入状态，保留祖先，未来对话和未来文件内容不进入新执行请求 |
| 手动编辑与原分支 | 手动源码与新增文件存入原分支，恢复预览取消不修改文件，切回后完整恢复 |
| 预览失效 | 预览后外部编辑返回 409，文件和会话树保持原样 |
| 停止与超时 | 在真实补丁落盘、下一次模型请求阻塞时取消，等待工作退出后保存最终实际文件状态 |
| 执行中进程被终止 | 重启标记 interrupted，快照摘要与遗留文件一致；查看历史不重放模型请求 |
| 恢复事务中进程退出 | 在文件替换后、进度确认前直接退出；重启阻止新任务，显式完成或回滚恢复 |
| SSE 与浏览器刷新 | 断开观察后任务继续，游标重放无重复，终态快照权威；刷新续看，不重复提交 |
| 输出未完成与停止后迟到输出 | 缺终止帧标记 failed；停止后的模型输出不能写入已结束回答 |
| 所有权与认证隔离 | 无效令牌拒绝、跨会话对象拒绝、跨进程同根或嵌套项目拒绝，进程退出释放锁 |

浏览器直接操作生产界面，检查真实验证计数、分支数量、手动修改提示与磁盘内容，
并保存截图。S5 的模拟执行器浏览器验收仍用于细粒度界面回归；S6 没有把它当作生产执行验收。

工作台保存的是执行收尾后的**实际状态**。在本次协作取消场景中，L2 会先回滚尚未验证的尝试，
因此取消轮次的 changed_files 为空；进程被强制终止时没有机会回滚，重启快照保留部分修改。
不能把“曾经写过补丁”误认为“终态仍然存在补丁”。外部编辑的保存与冲突处理仍遵循 S3/S4。

## 证据与故障定位

每次入口默认生成独立的 `.runtime-tools/s6-<UTC 时间>/`，也可使用
`--artifacts <新目录或空目录>`。拒绝覆盖已有证据目录。

- `report.json`：平台、解释器、实际命令、所选分组、耗时、退出码与整体状态。
- `api.xml`、`regression.xml`：对应 pytest 的 JUnit 结果；`<组>.log` 保存终端输出。
- `pytest/`：每个场景独立的源码、数据库、恢复日志与服务日志。
- `model/provider.jsonl`：合成测试请求的协议记录，便于核对工具结果及历史隔离。
- `browser/browser-results.json`、`browser/service.log`：浏览器场景结果和服务日志。
- `browser/*.png`：通过截图或失败截图；浏览器总超时会记录失败结果及已有服务日志。

这些目录可能包含测试令牌和合成源码，已由 `.runtime-tools/` 忽略，不进入 Git。
测试协议控制端点仅存在于 `tests/workbench_e2e_support.py` 的回环测试服务，生产 HTTP API 未增加控制接口。

## 本次发现与修复

真实 L2 停止场景暴露了运行进度锁的重入死锁：stop/close 持锁调用 token.cancel，
其同步回调发布恢复进度，再次获取同一把锁。线程堆栈定位后将运行进度门锁改为 RLock，
保留取消与终态提交的互斥关系。增加 stop/close 回调重入回归，并通过真实协议停止验收复验。

较深的证据目录还暴露了 Windows MAX_PATH 问题：SQLite 计划日志无法打开、
计划 blobs 子目录或 observation blob 写入失败，导致只读证据退回内存并阻止 L2 继续。
复用已有 native_io_path 在 SQLite 和 blob 的 I/O 边界使用扩展路径，保持项目与会话身份不变。
增加长路径计划日志与 blob 重新读取、observation 持久化与清理、UNC 路径拼写回归。

本次验证环境为 Windows、Python 3.14 和本机 Edge。完成记录见下节；
没有运行仓库全量测试，没有调用真实远端模型，也未验证容器执行层或 WSL 工作台服务。
协议测试证明执行与恢复契约，不代表真实模型的修复质量或其他供应商协议均已验收。


## 加固前完成记录（2026-10-12）

- 生产链路 API 验收 10 项全部通过；按失败与直接影响范围分批复验，原分支、手动修改、停止、超时、崩溃恢复均有证据。
- 生产链路浏览器验收 2 组通过：chat_protocol、project_lifecycle，使用本机 Edge。
- 工作台相关回归 126 项通过；计划、协调、观察、记忆与协作存储回归 74 项通过。
- SSE/祖先路径 JavaScript 测试通过；15 个新增或修改的 Python 文件 Ruff check 与 format 检查通过。
- 验收入口成功保存 JSON/JUnit/日志/截图；特意指定不存在的 Node 后返回 127 并记录 failed，确认缺依赖不会误报通过。

主要本地证据目录（均已忽略）：

| 证据 | 路径 |
| --- | --- |
| 修复与停止/超时/崩溃 4 项 | .runtime-tools/s6-20261012T043816-360311/ |
| 浏览器 2 组 | .runtime-tools/s6-20261012T043844-566193/ |
| 工作台回归 126 项 | .runtime-tools/s6-20261012T043940-634364/ |
| 其余 API 6 项 | .runtime-tools/s6-20261012T044053-179617/ |
| 存储回归 74 项 | .runtime-tools/s6-runtime-regression.xml |


## 加固后的验收

以上完成记录对应加固前的直接目录执行契约。当前版本采用独立工作区、受监督 worker、
候选明确应用与统一 SQLite，不能用旧的 host 修复验收替代当前 WSL 修复验收。
当前结果、未通过的环境门禁及证据见 [加固记录](WORKBENCH_HARDENING.md)。

新增入口：`python tests/run_workbench_e2e.py --group hardening`，仅运行相关加固测试。
浏览器 `--case hardening_ui` 使用明确的持久化测试数据验证候选/恢复/执行门禁，
不调用模型、不证明真实 Layer 2 修复能力。`project_lifecycle` 仍是实际受约束修复验收，
必须在 WSL 环境可用时单独执行，缺失要求计为失败，不退回 host。
