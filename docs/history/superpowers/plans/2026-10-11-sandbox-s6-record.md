> 历史设计／实施记录，仅供人工查看。当前流程见 docs/REPAIR_CONTRACT.md。

# S6：生命周期与资源保护

S6 提供任务绑定的关闭、崩溃恢复、跨 backend 重载、依赖回收和环境淘汰，并为实际工具进程树设置内核资源限制。工作区仍是用户仓库或已绑定 worktree；LLM 与可信控制面不迁入工具沙箱。生产默认门禁继续关闭，CLI 接入属于 S7。

## 生命周期

- `GuardedWindowsWorkspaceBackend.close()` 先关闭当前实例的准入，取消自己的活动调用，等待清理并核对持久 receipt；重复关闭安全。关闭保留会话绑定、审批、依赖、源码、worktree 与恢复证据。新 backend 只能复用相同的任务、环境和工作区身份。
- `recover()` 在跨进程 receipt 锁内，针对 workspace/task/run/call/runtime 的精确 execution key 清理原生 cgroup，写入反重放终态。没有写入意图的中断可确认为 cancelled；存在任何 mutation intent 时仍为 uncertain，保留原 owner/generation、意图和原结果，阻止重放及自动回滚。收到 uncertain/unverified 终态后也可以单独补充清理证据。
- 回滚归属从持久、已确认的提交 receipt 重建，不再只依赖进程内 `ToolContext`。恢复前仍进行版本比较，外部用户编辑不会被覆盖。
- `retire()` 是显式任务淘汰，要求没有活动 owner、未完成调用、结果不明的写入、待决审批或未消费授权。发布 `retiring` 标记与权限锁、SQLite owner 写事务串行，关闭新增审批和 owner 准入后才收集依赖。失败保持 retiring，可由新进程调用 `resume_retirement(binding)` 重试；原生清理确认后才能成为 retired。
- 原生 task lease 覆盖全部工作区工具和命令。任务退休仅删除对应的 Linux `deps`，保留 lease、反重放 tombstone、宿主任务状态及仓库/worktree。活动任务已满时，也允许为从未执行的任务创建退休证据，不创建依赖环境。
- 每个托管 backend 在可用前持久 pin 对应环境。即使会话空闲、宿主重启或等待审批，pin 也阻止环境回收。`SandboxEnvironmentManager.retire()` 持准备锁核对所有 pin 已 retired、发行版身份与 WSL 注册路径，再仅 unregister 自己拥有的精确发行版；不递归删除宿主路径。中断后可重试，安装和恢复日志保留。
- `Agent.close()` 接入 backend 关闭；修复编排在释放协调 owner 前关闭 backend。清理不明则保留 owner/工作区，并进入 recovery_required。

## 资源执行面

固定 root supervisor 只管理产品自身的 cgroup、日志和临时目录，不接收可执行文件或宿主路径参数，不操作用户工作区。controller、bwrap、FUSE 和工具进程在加入 execution cgroup、设置硬 rlimit 后降至 uid/gid 1000。root supervisor 留在该执行 cgroup 外，终态必须等待整个进程树清空。

| 保护对象 | 默认限制与行为 |
| --- | --- |
| 单次工具进程树 | cgroup v2：512 MiB RAM、swap 0、64 PIDs、1 CPU，OOM 按组终止 |
| 该发行版内的工具进程树合计 | 2 GiB RAM、swap 0、256 PIDs、2 CPUs；不包含 root 监督进程 |
| 单进程 | core 0、256 文件描述符、单文件 64 MiB、CPU 时间 120 秒 |
| 单次调用 | 请求 timeout 最多 120 秒，controller 执行超时与 root 监督截止时间共同约束 |
| 沙箱临时目录 | 64 MiB tmpfs 硬限制；每次执行有精确命名的原生 scratch，进程树清空后清理 |
| 任务依赖 | 256 MiB、8192 条目；执行期间采样检查，dispatch 时校验版本和预算，超限终止 |
| 输出与协议 | 沿用输出硬上限；root RPC frame 最多 2 MB；向宿主写入最多等待 5 秒 |
| 任务注册 | 最多 128 个活动原生任务、4096 个总任务目录/退休证据；退休证据不占活动槽 |
| 执行日志 | 单工作区任务 receipt 最多 8192 个；原生反重放 journal 最多约 32768 对记录，满后拒绝新增执行 |

依赖磁盘/条目限制是采样预算，**不是文件系统硬配额**，突发写入可能短暂超限。其磁盘硬配额、更细的整体容量管理可后续加强；tmpfs、cgroup 和 rlimit 已由内核强制。日志与退休证据不会被自动删掉以释放准入容量；达到证据上限时失败关闭，需要显式退休可回收的环境。

## 验证

- 专项测试覆盖精确恢复、带写入意图的阻断、原 owner/generation 保留、身份篡改、回滚归属重载、关闭取消、待决审批/活动 owner 阻止 GC、退休重试、新审批与 owner 并发栅栏。
- 已通过的相关批次：S6/permissions/task_context 97 passed；workspace backend/routing/S4 72 passed；协调与绑定批次除 Windows 长路径 pin 问题外通过，修正后对应 pin 与 receipt 4 passed；终态不明恢复与 receipt 5 passed。
- 收尾复验：S6 与 controller 28 passed；新增编排关闭顺序/失败保留 owner 两个用例通过；Windows 读句柄竞争、并发授权和 receipt 5 passed。只复验受改动影响的用例，没有运行全仓测试。
- 相关源文件、测试与两个 S6 probe 的最终 Ruff 检查通过；相关已跟踪改动的 `git diff --check` 通过。

最终当前源码的实机验收 50/50 项通过，环境健康检查 6/6 项通过，源码 manifest 与验收发行包一致。验收在独立真实 Git 仓库及 worktree 执行，测试了持久回滚归属、外部编辑保护、离线依赖重载、内存 OOM、fork/PID 上限、CPU 超时、临时盘 ENOSPC、输出上限、独立任务存活、close、宿主 SIGKILL、校验 `/proc` 身份后的 root supervisor SIGKILL、双重 fork/setsid 清理、审批与环境 pin 栅栏、幂等任务 GC。验收后没有 execution cgroup 或 scratch 残留，worktree、用户编辑和任务日志保留。

单独的临时 `s6-gc-v1` 发行版的预算与回收验收 17/17 项通过，验证活动任务和总证据预算、依赖字节/条目预算，以及真实环境 unregister 和幂等淘汰。该发行版已回收，其他发行版 inventory 不变。其验收发生在 Windows 日志替换重试修复前；该修复只作用于 Windows 分支，原生 Linux GC 路径没有后续变化。

## 构建与证据

- 最终环境：[2026-10-11-sandbox-s6-environment-accepted.json](2026-10-11-sandbox-s6-environment-accepted.json)
- 最终实机：[2026-10-11-sandbox-s6-probe-accepted.json](2026-10-11-sandbox-s6-probe-accepted.json)
- 独立原生预算/环境回收：[2026-10-11-sandbox-s6-gc-probe.json](2026-10-11-sandbox-s6-gc-probe.json)
- 完成审计：[2026-10-11-sandbox-s6-completion-audit.json](2026-10-11-sandbox-s6-completion-audit.json)
- release ID：`83ecffc0a46c7644ce9c7b8255a2a9282743c8bcdd97d2e2eaf705b3014cc31c`
- distribution：`FixLoop-Sandbox-s6-v1-83ecffc0a46c`
- runtime digest：`ffa81db422d58c3d21fa0674ff9c3465dd54bdf66bd03ff6ad4237d8529304af`

## 已定位的验收问题

第一次宿主崩溃测试没有先保存 SandboxTaskBinding，子进程尚未启动；随后补齐保存和错误日志，只续跑生命周期阶段。追加诊断误导入完整 Agent 包，实际报缺少 pydantic，并生成两个测试 pyc 缓存。诊断核对全部源文件 manifest 后仅移除这两个缓存，runtime digest 精确恢复到原构建值；诊断记录见 [2026-10-11-sandbox-s6-cache-diagnostic.json](2026-10-11-sandbox-s6-cache-diagnostic.json)。所有后续原生诊断固定 `-I -B`，没有安装额外依赖或放宽完整性检查。首次报告保留失败历史：[2026-10-11-sandbox-s6-probe.json](2026-10-11-sandbox-s6-probe.json)。

并发审批测试复现了 Windows 读取标记时 MoveFileExW 返回 WinError 5。针对实际 OS 读句柄补充失败复现后，将原子写穿替换增加最多一秒的短暂共享冲突重试，保留原临时文件与 durability，不通过删除旧标记规避锁。长路径使用扩展绝对路径。原生任务上限回收修正与 Windows 日志修正均已重新生成不可变发行包。

一次只读诊断因自动审批审查超时未执行，随后按工具要求重试一次成功；没有留下审批阻塞。旧版本环境和原有工作区改动保留，未提交、推送或创建 PR。
