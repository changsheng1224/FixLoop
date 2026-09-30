# FixLoop WSL 命令与测试沙箱 MVP 开发计划

日期：2026-09-30。依据：[MVP Spec](../specs/2026-09-30-wsl-command-sandbox-mvp.md)。状态：P0 环境诊断与 P1 独立后端已完成；P2–P4 未完成，产品沙箱未启用。证据见 [P0 记录](2026-09-30-wsl-command-sandbox-p0-record.md) 与 [P1 记录](2026-09-30-wsl-command-sandbox-p1-record.md)。

## 顺序与约束

P0 → P1 → P2 → P3 → P4，预计 8–12 个有效工作日。先确认隔离/清理机制，再接工具，避免完成工具迁移后才发现后台进程无法回收。

开始时记录当前工作树和相关源码哈希。已有未提交修改不 reset/覆盖，不用仅含 HEAD 的 worktree 冒充当前基线。隔离 checkout 与 Git 工作流按实际状态和 CLAUDE.md 处理；不自动 push/合并。

开发范围仅为一个 WSL2 发行版、原生 Linux 工作区、固定 Python profile、单并发命令/测试沙箱。每阶段先说明目标与模块。当前采纳授权产出文档，尚未开始实现或实机环境变更。

## P0：环境与威胁模型冻结（1 天）

产出：可执行的环境准备说明、执行入口审计、固定 profile 与阻塞条件。

任务：

1. 读取官方 WSL/bwrap 文档，记录实际 Windows、WSL2、发行版、内核、bwrap、Python/pytest 版本；不要自动启动/安装未知发行版。
2. 确认 WSL 原生 fixture/workspace、可信 controller/helper/toolchain 和独立 state_root，不引入 Windows 原目录同步。
3. 验证 user/mount/PID/network namespace、亲代退出联动、tmpfs 限额和 WSL 互操作边界。只用无害临时 fixture/哨兵。
4. 审计 run_shell、quick_test、验证策略、修复前后测试、Git、写后 lint 和静态验证等入口，列出 backend/可信数据处理/禁用的处理方式。
5. 明确外部 session/Observation/checkpoint 路径接入点，禁止把可信状态暴露在目标可写工作区。
6. 固定 profile、预算、工具清单、pytest 依赖/插件、开销报告判定方式。

验收：有一份可重放的 profile/preflight 记录；S2–S5 基础探测与临时存储限额具备实机证据。关键能力不可用时标 blocked，先报告具体限制，不能改为宿主执行。

预计实验/准备代码 80–140 行，记录文档另计。需要安装依赖或改全局 WSL 配置时按授权处理，不能把它当普通代码编辑。

## P1：最小 executor 与生命周期（2–3 天）

模块：`linux_sandbox/models.py`、`policy.py`、`supervisor.py`、`backend.py`、`receipts.py`。

任务：

1. 定义请求、结果、错误码、收据和 policy digest；结构化 stdin 请求，模型不控制 supervisor/bwrap argv。
2. 固定挂载/env，实际 preflight，工作区映射及可信 helper 启动；保证 controller 不从任务目录导入代码。
3. 启动前原子登记 planned，进入目标前确认 running；执行后有界收集输出、清理 namespace/后台进程和临时存储，生成 terminal 收据。
4. 独立 deadline、控制 pipe EOF、TERM/KILL 与清理确认；不依赖下一次调用才回收。
5. 输出洪泛与 tmpfs 满处理；控制 fd 不传入目标；任何清理未确认返回 uncertain。
6. 按工作区独占锁实现单并发；启动 reconcile 只处理已验证本 scope 的 registry，防 PID 复用误杀。

测试：新增 `test_linux_sandbox_protocol.py`、`test_linux_sandbox_lifecycle.py`、`test_linux_sandbox_integration.py`；包含错误请求、超限、父进程退出、setsid/double-fork、管道持有、控制器与 supervisor 被杀。

验收：S1/S6–S9/S12 的独立后端实机测试通过。尚未接 ToolExecutor 也能证明启动/清理机制。若只能 killpg 而无法处理脱离进程组的子进程，本阶段不完成。

预计实现 350–500 行，测试 150–240 行。

## P2：工具与最终验证统一接入（2–3 天）

模块：`tool_context.py`、`tools.py`、`tool_executor.py`、配置/CLI、`src/tools/spec.py`/manifest/composite、`repair_factory.py`、`orchestrator.py`、`repair/verification/verify.py`、修复前后测试入口。

任务：

1. 配置执行开始固定 wsl_bwrap backend；拒绝冲突的 tier/fallback 配置，不混用 auto Docker→host 路径。显式 skip-verify 仅表示未验证，不得报告通过，也不能作为 P4 修复闭环的验收配置。
2. ToolContext 注入 backend，run_shell 保留现有命令校验/审批后传 argv；quick_test 验证路径与 nodeid并固定 pytest 参数。
3. 新增 BwrapVerifyStrategy，最终 pytest 与修复前后测试全部共用 backend，测试结果细分 passed/failed/environment/interrupted/no-tests。
4. 校验执行入口清单：项目可执行入口接 backend 或禁用；可信 rg/纯 AST/快照的例外显式登记；未知工具类别拒绝。
5. 工具 registry/spec/phase 权限/执行类别/ToolResult metadata 同步，execution_tier 写实际 linux_sandbox。相关 prompt cache signature 稳定。
6. 每次项目执行按潜在副作用处理，复用快照/变更逻辑；清理未确认前阻止新的命令和文件写入。
7. 固定 Windows launcher 只启动 WSL 控制入口；不把用户任务拼进宿主 shell。

测试：现有 `test_tools.py`、`test_tool_executor.py`、`test_tool_runtime_contracts.py`、`test_cli_repair.py`、`test_cli_exit_codes.py`、`test_repair_factory.py`、`test_tools_manifest.py`、`test_phase_b_tools.py`、`test_repair_tool_schema_stable.py` 中相关用例；新增 `test_bwrap_tool_routing.py`、`test_bwrap_verify.py`。

验收：S8/S13 路由与真实执行通过；假 backend 验证调用契约，实机 trace 验证最终测试实际进沙箱。禁止只检查 metadata 字符串就认定已隔离。

此阶段集成测试使用已分离的临时控制状态。完整运行时的 state_root 接入在 P3 完成前，profile 不对普通任务开放，不能先暴露工作区内的旧 session/checkpoint 再补保护。

预计实现 240–380 行，测试 130–220 行。

## P3：最小恢复、状态隔离与事件（1–2 天）

模块：`checkpoint.py`、`session_contract.py`、`session_store.py`、`context_runtime.py`、Agent 生命周期与 action ledger、Canonical Trace、registry/reconcile。

任务：

1. 接入可信外部 state_root；session/checkpoint/Observation/收据均不暴露给目标，既有文件工具不能修改控制状态。
2. 在已有受保护 payload 中记录 backend/policy/distribution/mapping/call/receipt checksum；不保存可直接恢复的 PID。
3. 恢复先 preflight 与 reconcile，running/uncertain 清理确认后检查文件变化，不自动重跑或自动回滚。
4. 工具 step resume/idempotency 同样拒绝重放未知副作用。planned 状态不能单独证明目标未执行。
5. 收据损坏、身份变化、旧 PID 被复用时明确拒绝，不误杀；本工作区未知状态阻止新执行。
6. 接入启动/取消/清理/失联/恢复事件；输出脱敏，凭据不进入日志。

测试：新增 `test_bwrap_receipts.py`、`test_bwrap_resume.py`；现有 `test_checkpoint_resume.py`、`test_strong_step_resume.py`、`test_session_bak.py`、`test_observation_store_governance.py`、`test_canonical_trace.py` 中相关用例。

验收：S3/S10/S11，通过真实部分写入+强制终止+恢复案例证明不重跑；目标无法修改可信状态；损坏收据不会被静默接受。

预计实现 150–250 行，测试 130–210 行。

## P4：隔离回归、开销与演示（2 天）

产出：实机测试矩阵、逐次开销数据、三个 Demo、一个真实修复闭环及限制说明。

任务：

1. 建立 `tests/fixtures/linux_sandbox/` 无害命令/哨兵，运行 Spec S1–S13。case 在独立工作区执行，失败也保存记录。
2. 使用宿主受控监听器验证网络：沙箱外连接正例、沙箱内固定 IP 连接负例；不用真实凭据或随机外网服务判断隔离。
3. 进程用启动时间/namespace 身份及心跳哨兵交叉核对；保留 controller/supervisor 强制终止案例。
4. 同 WSL 同工具链跑 trusted_host_linux 与 wsl_bwrap 的四类无害任务，各预热一次、至少 10 次；显式基线不进入 production fallback。
5. 保存 startup/execute/cleanup/total、output/status/error、版本和 policy digest，报告 median/p95 与失败记录。
6. 真实 Agent 在专用 repo 修复一个已知通用问题，确认 quick_test 与最终 pytest 都有 backend 收据。真实模型调用遵循授权范围，未执行时明确 pending。
7. 编写三段 Demo、威胁模型更新和面试讲解：为何不只靠 allowlist、何时 uncertain、为何恢复不重放、哪些边界未覆盖。

建议新增命令入口（实现完成前不可当现有命令使用）：

```text
python -m agent_runtime.linux_sandbox preflight --config <trusted-config>
python -m src.eval.sandbox_mvp --config <trusted-config> --suite isolation --output <output-dir>
python -m src.eval.sandbox_mvp --config <trusted-config> --suite overhead --repetitions 10 --output <output-dir>
```

测试入口在固定可信环境运行；不接受模型给出的任意攻击脚本、PID或系统配置改动。

建议结果目录：

```text
eval_results/wsl_sandbox_mvp/<run-id>/
  environment_manifest.json
  policy.json
  cases.jsonl
  receipts/
  traces/
  overhead.jsonl
  report.md
```

每条 case 记录 case_id、fixture hash、task/run/call、环境与 policy digest、预期/实际结果、收据/trace、cleanup 证据及诊断。报告不能以 mock、skip 或文档描述替代实机证据。

预计评测代码 100–180 行，fixture/报告另计。

## 完成门禁

- 单一 profile 的 WSL2/bwrap 实机能力与版本可重现，缺失能力时失败关闭。
- 所有可达项目执行入口走 backend 或被禁用，最终 pytest 无宿主备用路径。
- 文件工具仍为可信本地处理，产品和面试材料如实说明未迁入 OS 沙箱。
- 挂载、网络、WSL 互操作及控制状态隔离有实机负例与必要正例。
- 超时、取消、失联及后台进程清理可验证，未确认则 uncertain。
- checkpoint/step resume/idempotency 不盲目重放未知副作用。
- S1–S13、逐次开销数据与真实修复闭环具备证据。
- 相关测试和 lint/format 完成；全量测试需另外显式授权，未运行不声称通过。
- 不把网络禁用/PID namespace 描述为内存、CPU、fork-bomb 或内核攻击的完整防护。

## 范围控制

P0 能力不满足先停在明确的环境阻塞，不先做大量工具迁移。P1 进程清理未通过先修生命周期，不追加第二后端。P2 不借机迁移全部文件工具或 Docker Harness。P4 只报告实测开销，若超出约定预算先分析每调用启动成本，不直接新增常驻沙箱与并发服务。
