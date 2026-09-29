# FixLoop WSL 命令与测试沙箱 MVP Spec

日期：2026-09-30。状态：用户已采纳 MVP 范围；本文是开发契约，不代表实现或隔离验证已经完成。

## 1. 目标、范围与交付

让 FixLoop 在 WSL2 内运行，并把不可信命令、quick_test 与最终 Python 测试交给统一 bubblewrap 执行后端。环境不足时明确拒绝，取消后确认进程范围已清理；通信中断后不盲目重跑，恢复时先核查执行收据和工作区。

交付一条可演示链路：任务 → ToolExecutor/验证策略 → Linux supervisor → bwrap → 目标进程 → 清理确认 → 执行收据/变更检查。

首期保留：

- 固定一个 WSL2 发行版、一个 Python 工具链 profile、Linux 原生任务工作区。
- 默认禁网、只读工具链、独立临时目录、受限环境变量。
- run_shell、quick_test、最终 pytest 的同一执行后端。
- 单并发、后台子进程清理、超时取消、失联后的不确定状态。
- 最小 checkpoint 身份与收据校验，禁止自动重放未知执行。
- 固定隔离/恢复案例和执行开销测量。

首期不做：

- Windows 工作目录挂载、DrvFs 工作区、双向副本同步、每工具跨 wsl.exe 执行。
- 全部文件工具迁入 bwrap；跨工具沙箱常驻会话。
- 多语言、多发行版兼容矩阵、运行时依赖安装、网络放通。
- 并发执行、持久后台任务、完整执行恢复框架。
- CPU/内存/进程数的 cgroup 硬配额、多租户隔离、内核/虚拟机攻击防护。
- 替换整个现有 Docker Harness，或宣称新增后端比 Docker 更安全/更快。

预计单人 8–12 个有效工作日，含相关测试与集成返工。规划约 1,400–2,300 行实现加测试，不作为验收指标。当前机器能力、bwrap 选项、WSL namespace 行为必须在 P0 实测。

## 2. 信任边界

可信：WSL 内 FixLoop 控制进程、supervisor、bwrap、准备好的工具链、开发者配置及控制面状态。

不可信：模型参数、仓库代码、pytest 插件/conftest、命令及其输出、任务工作区内的文件内容。

文件工具、AST 解析、快照和内容 hash 仍由可信运行时处理，继续经过现有路径、敏感文件、字节上限、EditLock 和写入审批。这些不获得 OS 沙箱隔离承诺。run_shell 中直接执行 Python/pytest 不得绕过后端。

任务工作区必须是无凭据的专用 worktree/副本，不能用用户整个 home 或 FixLoop 控制程序目录。MVP 控制面状态存于工作区外，并且不挂入目标进程；该模式下 session、checkpoint、Observation、收据和 supervisor registry 均使用配置的外部 state_root。未完成状态路径分离时不能启用 profile。

工具链和 helper 不得位于可写工作区。helper 从显式可信位置运行，不通过工作区 PYTHONPATH 或同名模块加载；目标 Python 可以加载仓库代码，控制进程不能因此加载仓库插件。

工作区是允许持久化修改的目录；隔离 /tmp 和 /home/sandbox 是额外的临时可写区域。文件工具的敏感路径拒绝不会自动限制任意程序读取仓库内文件，因此使用前验证禁止的凭据/控制目录；发现这些资产时拒绝 profile，而不是假称其已受保护。

策略保护进程的文件系统视图、网络和可见进程范围，不能描述为绝对安全边界。工具链可读，故验收说法是“非授权用户文件不可见”，不是“工作区外所有文件都不可读”。

## 3. 运行方式与后端选择

Windows 启动器仅接受受信任配置，使用结构化 argv 启动：

```text
wsl.exe --distribution <trusted-distro> --exec <trusted-python> -I <trusted-launcher> <trusted-config>
```

这是启动接口草图；实际路径及 WSL 版本在 P0 固定。用户任务通过标准输入/受控任务接口传递，不拼接进 PowerShell、cmd 或 Linux shell 启动串。启动器不负责模型命令转义或逐工具执行。

固定受信任配置：

```text
execution_backend = "wsl_bwrap"
sandbox_policy_version, distribution_id
workspace_root                 # WSL 原生、规范化的真实 Linux 路径
controller_root, helper_path, state_root
bwrap_path, toolchain_profile   # 固定 executable/mount/env 白名单
limits                         # 模型参数只能降低上限
```

Windows 原目录与 /mnt/c 等映射在本版拒绝；工作区必须由显式准备流程建于 WSL 原生文件系统。普通 native Linux 可用于单元测试，但首期实际支持与发布证据只覆盖指定 WSL2 环境。

执行开始固定 backend，不在运行中自动切换。在 wsl_bwrap 模式，Docker/host/static 验证不作为备用路径；与现有 --execution-tier/--require-sandbox 冲突时启动前明确拒绝。旧后端只在用户显式选定的独立旧模式运行，不属于此模式的失败降级。

LSP 等长期外部服务不在本 spec 内；本 profile 首期禁用代码探索 LSP，不把已有检索计划自动视为与该沙箱兼容。

## 4. 模块与接口

通用后端属于 L1，不能 import src。建议新增：

```text
agent_runtime/linux_sandbox/
  models.py       # 请求、执行结果、收据、错误码
  policy.py       # 信任配置、挂载/环境与 preflight
  backend.py      # execute/cancel/reconcile，调用范围控制
  supervisor.py  # 独立 Linux 进程、管道、deadline 与清理
  receipts.py    # 原子收据、锁、恢复身份检查
```

接入点：ToolContext 注入 backend；tools.py 中 run_shell/quick_test 调用它；ToolExecutor 保留审批、预算、结果规范化；L2 新增 BwrapVerifyStrategy，通过 repair_factory 和 Orchestrator 的显式路由调用同一 backend。

```text
SandboxRequest:
  schema_version="1", workspace_id, task_id, run_id, call_id
  operation: command | pytest
  argv[], cwd_relative, timeout_s, output_limit_bytes
  policy_digest, workspace_mapping_id

SandboxExecutionResult:
  execution_status: completed | rejected | start_failed | timeout | cancelled | uncertain
  exit_code?, signal?, error_code
  sandbox_id, receipt_id, requested_backend, actual_backend
  stdout_excerpt, stderr_excerpt, output_truncated
  duration_ms, startup_ms, cleanup_ms
  cleanup: confirmed | failed | unverified
  mutation_status: checked | pending | unknown
  affected_paths[], workspace_diff_ref?
```

模型不能提供原始 SandboxRequest，也不能指定挂载/发行版/bwrap 参数。run_shell 继续先执行现有命令校验和 parse_shell_argv，再由控制器构建请求。目标 argv 通过 JSON stdin 传给可信 supervisor，不通过 shell 展开。首期不支持 shell 管道、重定向、后台运算符；后台进程测试用固定 Python fixture 派生子进程。

cwd 必须经 ToolContext.resolve 验证并映射到 /workspace 下，拒绝外部和穿越路径。quick_test 将 nodeid 分成文件与 :: 后缀，先验证文件路径，再重组相对目标；不接受 pytest 开关冒充 nodeid，测试选项由控制器固定。

结果 metadata 必须经过现有执行器透传，不能仅放在未透传的 data 字段。新增 execution_tier="linux_sandbox"，明确区别于 host/container；顶层 ToolResult.status 使用既有枚举，细分状态在 metadata 中保存。

## 5. 挂载、环境与 preflight

挂载 profile 必须显式列举并计算 policy digest：

| 路径 | 策略 |
|---|---|
| /workspace | 本次任务目录；命令/测试允许修改，受收据检查 |
| /toolchain 及必要 loader/lib 路径 | 只读，固定依赖闭包，不暴露整个 /usr 或用户 home |
| /tmp、/home/sandbox | 每调用独立 tmpfs，调用结束销毁 |
| /proc | 仅隔离 PID namespace 的 proc 视图，不暴露宿主 proc |
| /dev | 最小必需设备，不 bind 整个宿主 /dev |
| /mnt、/run、宿主 /home、控制 state_root | 不挂载 |
| workspace/.git | 目标侧不可写或隐藏；首期 git 模型工具禁用 |

不继承现有 fd、Docker socket、SSH agent socket或 WSL 互操作 socket；目标进程只获得必要标准流。supervisor 控制管道和收据 fd 禁止传给目标。

环境通过 clearenv/显式 env 生成。白名单起点是 PATH（仅沙箱工具链）、HOME=/home/sandbox、TMPDIR=/tmp、LANG/LC_ALL 和必要 Python 设置。不得继承 API Key、代理、宿主 PATH、WSLENV、WSL_INTEROP、PYTHONPATH、用户配置和缓存路径。

pytest 首期关闭第三方插件自动加载，所需插件只能由固定 profile 白名单显式启用；仓库 conftest 仍在沙箱内正常执行。仓库请求安装依赖只返回环境准备诊断。

preflight 不是 which bwrap：必须实际创建 user/mount/PID/network namespace，验证挂载读写、工具链启动、PID 管理与网络隔离。失败返回稳定错误码，没有 fallback。digest 包括发行版、workspace 真实路径/身份、工具链标识及策略/限制，不只 hash 配置显示名。

WSL 互操作验收覆盖绝对 Windows executable、cmd.exe/wsl.exe、继承路径、WSL socket 和相关 proc/binfmt 接口。清理 PATH 不算验证成功。若最小挂载与 proc 配置仍不能阻断互操作，P0 标为阻塞，不能自动修改整台发行版配置后宣称 MVP 可运行。

## 6. 能执行项目代码的入口清单

MVP 必须先审计所有可达执行路径。规则是：已注册项目执行入口走 backend；未知项目执行入口在该 profile 禁用。

| 入口 | 本版处理 |
|---|---|
| run_shell / quick_test | 统一 backend |
| 最终 Verifier pytest、修复前后 pytest、重试相关测试 | 统一 backend；环境失败不当作修复失败 |
| Docker sandbox_build/test/verify、pip install | 此 profile 禁用，不静默切换 Docker |
| git_blame / git_diff 等模型工具 | 首期禁用，避免仓库配置/外部程序旁路 |
| 静态验证/写后 lint 中可能启动解释器或其他程序的入口 | 明确证明只处理数据，或接 backend，或禁用 |
| grep 的固定 rg 后端 | 可信数据处理例外：固定可信 binary、argv、env，无 repo executable/config，路径策略不变 |
| 文件工具、AST parse、快照/hash | 可信数据处理，保留现有保护，不执行源码 |

固定 rg 等例外不等于允许通用宿主命令。ToolExecutor 增加/校验工具执行类别与 backend 适配，未知类别默认拒绝；canonical schema 在该次运行稳定，不在阶段内动态改工具列表。完成入口清单后才能宣称“项目命令与测试无宿主旁路”。

run_shell、pytest 能修改工作区；文件工具 EditLock 不能自动约束任意代码写入。所有项目执行按可能产生副作用处理，沿用 ToolExecutor 授权和快照/变更检查，不把只读工具标记当作实际写保护。

## 7. 子进程、失联与限制

并发上限为 1：每个工作区持有可信状态目录里的独占锁，跨多个 FixLoop 实例也不能同时执行。同任务被取消时不清理其他工作区/任务。暂不允许文件写工具与命令同时修改该工作区。

每调用启动一个独立 supervisor，在进入目标程序前原子登记 call_id 和启动意图。以独立 PID namespace、namespace init/reaper 及 bwrap 亲代退出联动为进程范围设计起点；具体选项以 P0 能力探测和破坏测试结果固定。

普通 killpg 不作为完整清理保证：测试必须包含 setsid、double-fork、父进程先退出、子进程持有输出管道。目标完成后也清理剩余后台进程，再完成收据。不能只等父 PID 返回。

supervisor 的 deadline 独立于控制器。控制管道 EOF 触发清理，不依赖 Windows 发送取消，不依赖下一次调用。supervisor 本身被强制终止时，也必须实测 namespace/亲代退出机制能回收目标；未确认前结果只能 uncertain。

取消/超限/失联流程：停止接受新请求 → TERM → 有限宽限期 → KILL → 等待 namespace/进程范围终止和管道关闭 → 清理临时存储 → 原子收据。stdout/stderr 始终并行有界排空，禁止先等待进程再 communicate 造成管道阻塞。

| 限制 | 默认值与性质 |
|---|---|
| 命令时间 | 20 秒；模型只能降低受信任最大值 120 秒 |
| quick_test 时间 | 60 秒；受信任最大值 120 秒 |
| 最终 pytest 时间 | 120 秒；profile 可预先设定更低值 |
| TERM 宽限 | 1 秒 |
| 清理确认期限 | KILL 后 3 秒；未确认则 uncertain |
| stdout+stderr | 合计 1 MiB；超限终止，不只截断显示 |
| 模型可见输出 | 合计 16 KiB，进一步服从现有上下文限制 |
| 临时写入 | tmpfs 合计目标上限 64 MiB；需确认实际 enforcement |
| 并发 | 1 |

tmpfs 大小若当前 bwrap/环境无法提供可验证硬限制，preflight 拒绝该 profile，或由用户另行采纳降级后的范围；实现不能把轮询监测伪装为硬限。CPU/内存和 PID 数量硬配额不在首期承诺中，fork-bomb/资源 DoS 防护不作为已实现能力。

下一次启动先 reconcile 本 workspace registry：依据 boot_id、进程启动时间、namespace 身份与 call_id校验清理对象，不能仅凭旧 PID 发信号。其他任务记录或身份不能确认时不误杀；本工作区保持拒绝执行并报告 uncertain，等待显式处理。

## 8. 收据、文件副作用与状态

可信 registry 在启动前写 planned，supervisor 随后写 running，清理后写 terminal；写入使用原子替换，记录 checksum。收据不包含可执行命令恢复指令，argv 可保存脱敏摘要/哈希。

receipt 最少字段：call/task/run/workspace ID、backend、policy digest、mapping ID、controller 与 distribution 标识、started_at/ended_at、execution_status、exit/signal、cleanup、输出截断/限额、workspace_diff_ref、error_code。

内部 registry 可以保存经验证的进程身份用于清理，但它不是 checkpoint 可恢复句柄，不能用模型提供 PID 或任意进程范围执行清理。

| 情况 | ToolResult 语义 |
|---|---|
| 命令 exit=0，清理与副作用检查完成 | success |
| 命令非零，清理确认 | error，保留退出码 |
| pytest exit=0，收据完整 | 测试 passed |
| pytest exit=1，收据完整 | 测试 failed，不与环境故障混合 |
| pytest exit=2/3/4/5 | 分别报告中断/内部错误/用法错误/无测试；不能标 passed |
| preflight/策略拒绝、工具链缺失 | rejected/start_failed，未执行项目代码 |
| 超时且清理确认 | timeout，映射 error；副作用仍需检查 |
| 用户取消且清理确认 | cancelled；不能据此宣称文件已回滚 |
| 通信中断、收据缺失/不符、清理未确认 | uncertain，禁止自动重跑 |

遇到断连即使随后发现 exit=0，也要验证完整收据与工作区变化后才能报告已完成。验证结果 all_passed 仅由完整、可信的实际测试结果决定。

项目执行前后沿用现有快照与变更清单。清理未确认时不得边执行边做最终 diff 或自动回滚；先阻止新执行。清理确认后计算变化，部分修改如实记录，由已有恢复策略或 Agent 决定下一步。状态不确定不代表无副作用。

## 9. Checkpoint 与恢复

最小持久化：backend、policy digest、distribution identity、workspace mapping ID、call_id、receipt_ref/checksum、执行状态。通过现有受 checksum 保护的 runtime_control/action_ledger 等扩展点保存，避免新增一套平行 checkpoint。

恢复流程：

1. 验证现有 checkpoint 身份、checksum 和工作区。
2. 重跑 sandbox preflight，比对 backend/policy/toolchain/mapping/distribution。
3. reconcile registry 与收据；running/planned/缺失终态先做清理与变化检查。
4. 未执行的 planned 只有可信证据确认“未启动”才能重新提交；不能仅按名称 planned 推断未执行。
5. in-flight/uncertain 调用不自动重放，返回结构化观察供 Agent 检查工作区；旧工具步骤 resume 同样遵守这一规则。
6. 只有已确认完成且收据一致的结果可作为历史结果复用，不能恢复原进程或后台会话。

环境变化返回 resume_policy_mismatch；无法确认旧范围清理返回 process_cleanup_failed/uncertain。相同 idempotency key 不能自动重试一个未知副作用调用。

## 10. 错误码与事件

错误码至少包括 wsl_unavailable、distribution_mismatch、bwrap_unavailable、namespace_unavailable、workspace_mapping_rejected、policy_denied、toolchain_unavailable、interop_isolation_failed、network_isolation_failed、tool_start_failed、tool_timeout、tool_cancelled、output_limit_exceeded、temp_limit_exceeded、process_cleanup_failed、execution_uncertain、receipt_invalid、resume_policy_mismatch、workspace_busy。

扩展现有 Canonical Trace：sandbox_preflight、sandbox_start、sandbox_end、sandbox_cancel_requested、sandbox_cleanup、sandbox_reconcile、sandbox_resume_check。携带 task/run/call、policy/backend、发行版标识、耗时、exit、cleanup、truncation、错误码和 receipt_ref，不输出环境变量全集、完整凭据或原始源码。

## 11. 实机验收矩阵

使用专用临时仓库和外部哨兵文件，不读取真实用户凭据。

| ID | 场景 | 验收结果 |
|---|---|---|
| S1 | 工作区读写、固定 Python/pytest | 修改可见，工具链只读，收据完整 |
| S2 | ../、绝对外部路径、外部 symlink | API 拒绝或沙箱不可访问；不泄漏哨兵内容 |
| S3 | 用户 home、控制 state、SSH/云目录、Docker socket | 不可见，控制记录不能被目标修改 |
| S4 | 外网、局域网、受控宿主监听器 | 无法连接；不仅以 DNS 失败作为证据 |
| S5 | Windows 绝对 executable、互操作 socket、binfmt/proc | 不能调用 Windows 命令越界 |
| S6 | 超时、取消、后台进程、setsid/double-fork | 父子范围终止，无持续心跳写入/残留进程 |
| S7 | 控制器被杀、supervisor 被杀、输出管道断开 | 有界自主回收；未知结果记 uncertain |
| S8 | bwrap/namespace/工具链缺失 | 稳定错误，不启动 host/Docker/static 备用执行 |
| S9 | 输出洪泛、临时写满 | 硬限触发并回收；诊断准确 |
| S10 | 崩溃中部分写入后恢复 | 变更可见，清理先于核查，未知调用不自动重放 |
| S11 | 策略/工作区/发行版变化、伪造或损坏收据 | 拒绝旧执行身份，不按旧 PID误杀 |
| S12 | 第二调用竞争、其他工作区任务存活 | 拒绝/排队符合单并发策略，取消不误杀其他任务 |
| S13 | 入口清单路由与真实修复 | quick_test/最终测试都进 backend，无项目执行旁路 |

网络负例在沙箱外先做受控连接正例，再在沙箱内验证失败，优先用本机监听器/固定 IP，区分隔离与外部服务不可用。进程残留检查同时使用可信 namespace/进程身份和心跳哨兵，不只 grep 进程名。

实机测试是完成门禁。mock 只验证协议/状态，不替代 namespace、网络、WSL 互操作及进程清理证据。

## 12. 开销对照与演示

同一 WSL 环境、工具链和无害 fixture，对照 trusted_host_linux 与 wsl_bwrap。宿主基线仅由显式评测入口运行，不通过沙箱模式失败降级生成。Windows 原流程可附录测量，不与 Linux 同环境开销混为一项。

任务：Python 无输出启动、读写小文件、少量 pytest、输出较多的命令。每项预热一次、至少 10 次记录；冷启动另列，不能排除失败和清理耗时。固定预算、文件内容、依赖、后台状态与版本。

保存每次 startup/execute/cleanup/total、输出字节、exit/status、preflight 成本和失败诊断。报告 median、p95 与逐次结果；10 次的 p95 只作描述，不声称统计稳定。用 P0 约定的时间预算与典型任务耗时解释开销，不预设提升百分比。

至少三个 Demo：越界及网络阻断；超时/取消清理后台进程；部分写入后崩溃恢复不重跑。另保留一个真实 Agent 修复任务，证据关联工具执行、修改及最终沙箱测试。

## 13. 验证与参考

遵守 CLAUDE.md：相关测试及 lint/format；全量测试需用户显式授权。安装依赖、修改 WSL 全局配置、远端操作与真实模型调用按会话授权范围执行。本轮只产出文档，不自动实施环境准备。

官方参考（本轮网络工具未能打开；P0 必须阅读并核对实际版本）：

- [WSL 互操作](https://learn.microsoft.com/en-us/windows/dev-environment/wsl-interop)
- [bubblewrap README](https://github.com/containers/bubblewrap/blob/main/README.md)
- [bubblewrap 手册源文件](https://github.com/containers/bubblewrap/blob/main/bwrap.xml)

实现必须以实际 bwrap 支持的选项和可复现实机记录为准，不以本文的能力要求代替证明。
