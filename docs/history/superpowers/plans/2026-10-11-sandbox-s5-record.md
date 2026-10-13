> 历史设计／实施记录，仅供人工查看。当前流程见 docs/REPAIR_CONTRACT.md。

# S5：权限与审批

日期：2026-10-11。目标是把沙箱权限收敛为任务绑定的持久 authority，并让需要升级的操作在精确调用边界暂停，等待可信 UI/API 决策。

## 已实现

- `PermissionScope` 将 workspace read/write、Python、pytest、离线安装以及可信数据/控制工具分为独立 capability；路径范围按路径分量匹配，禁止前缀误匹配和路径逃逸。
- `PermissionService` 持久化 identity、scope、revision、pending proposal、一次性 authorization ticket、消费调用 ID 和审计记录。状态文件位于 workspace 外的受信 state root，并带 checksum。
- 审批 proposal 对完整调用做 digest，包括 argv、timeout、owner/generation/coordination revision 和输入版本。回调只能返回 True/False/None；EOF 保持 pending，取消/中断使 proposal stale。
- scope grant 只有在 native backend 消费同一 ticket、同一 input versions 和同一 authority 后才提交；revocation、owner 变化、scope 变化和 CAS 输入变化都会拒绝恢复。
- `ToolExecutor` 的 sandbox Gate 2 使用固定 canonical access policy，忽略模型篡改的 registry handler、execution tier、isolated-read 和 terminal 标志。未经审计的 host 工具不能通过 registry 注入。
- workspace/command/pytest/离线安装统一经过授权与 backend；审批拒绝和等待在 backend 调用前返回 `not_started`，不会产生宿主快照或工作区副作用。
- AgentLoop、checkpoint、PlanSession 和 recovery 保持原 attempt 为 `waiting_approval`，恢复时重新检查 proposal、identity、owner、generation、协调 revision 与 workspace 状态。
- coordinator 资源注册失败会丢弃授权票据；native backend 返回缺少已消费 permission evidence 的成功样式结果会被标记为 `authorization_unconfirmed`；排队取消始终产生稳定 `cancelled` 错误码。

## 验证

相关测试通过：

- `tests/test_sandbox_permissions.py`：收尾核验 37 passed（包含 native batch 延后调用、sealed checkpoint、owner/revocation/cancellation 和 L2 取消顺序）
- `tests/test_bwrap_tool_routing.py`、`tests/test_tool_executor.py`（S5 直接用例）：11 passed
- `tests/test_native_tool_batch.py`、`tests/test_agent_loop.py`：74 passed
- Plan/checkpoint/workspace routing/backend 回归：191 passed；修复排队取消竞态后该用例单独通过
- Ruff（S5 修改文件及 probe）：通过

真实 backend probe `scripts/wsl_sandbox_s5_probe.py` 已通过：`2026-10-11-sandbox-s5-probe.json` 为 `passed`，真实仓库和 Git worktree 合计 58 项检查全部为 true。构建记录 `2026-10-11-sandbox-s5-environment.json` 为 `ready`，与验收记录身份一致：

- release ID：`a79ed7275183cb4be65c74a521bc9e431a2d4cd16a13c8a5f58134d9d37266fa`
- distribution：`FixLoop-Sandbox-s5-v1-a79ed7275183`
- runtime digest：`9242493b63a0ca9c728d32ed600bb2d1208d5e66742f47876f71b3e20aa9814d`

两份持久 authority 经 `PermissionService` 重新读取，与报告快照逐项一致，revision 均为 13，pending 均为 consumed，没有未消费票据。仓库 7 对、worktree 8 对 started/outcome 审计按 authorization ID 一一匹配。审批后命令仍无法访问宿主路径、产品控制通道和 WSL interop，源码及依赖只读，网络连接被拒绝。通用 shell 和 URL 安装均在 dispatch 前拒绝；URL 路径可能先被路径校验拒绝，验收同时接受 `policy_denied` 与 `workspace_mapping_rejected`，并验证 receipt registry 未变化。

原先的 `environment_command_failed` 已解决。后续 worktree 隔离用例失败的直接原因是 probe 内嵌 Python 有多余缩进，实际返回 `IndentationError`，尚未执行隔离断言。修正为 `textwrap.dedent`，先检查代码可编译，再运行真实 backend；没有放宽 runtime 或 OS 策略。恢复入口改为以 `pytest_auto_allowed` 判断审批/安装阶段已完成，以 `authority_reload_final` 跳过已完整通过的标签，同时校验 release ID、runtime digest 和 distribution，避免在已修改的 worktree 重放写入。原调用和四次阻塞/失败快照均保留在 `previous_runs`。

接续执行访问 AppData 已安装环境时曾被当前执行器的文件权限阻止；使用自动审查允许的提权访问后完成真实验收。该失败也保留在历史中。S4 已通过的发行版和验收记录未覆盖。

收尾 lint 发现 `tool_batch.py`、`tool_batch_runtime.py`、`tool_step_runtime.py` 三处超长行，仅调整换行；直接涉及的 native batch、原调用恢复和终态路由复验 5 passed。S5 相关 23 个源文件/测试/probe 的 Ruff 检查通过，权限测试和 probe 的格式检查通过。由于包内全部 Python 源码参与发行包哈希，修正后另行生成发行包，并在独立真实仓库/worktree 完成新包验收，保留上述已通过记录。

### 最终发行包与当前源码核验

- 构建报告：[2026-10-11-sandbox-s5-environment-final.json](2026-10-11-sandbox-s5-environment-final.json)，状态 `ready`，6 项环境健康检查全部通过。
- 实机报告：[2026-10-11-sandbox-s5-probe-final.json](2026-10-11-sandbox-s5-probe-final.json)，状态 `passed`，58 项检查全部为 true。
- 完成审计：[2026-10-11-sandbox-s5-completion-audit.json](2026-10-11-sandbox-s5-completion-audit.json)，状态 `passed`。
- release ID：`e5df2310a970a8f264f73dd3e3668e2776ff2f6adb3c21d0fd16f579248fd1ff`
- distribution：`FixLoop-Sandbox-s5-v1-e5df2310a970`
- runtime digest：`164b04dc611d77911812f236122924434c759f02daefc0887b61a014fc522be7`

当前 `runtime_files()` 与 release manifest 重新计算的 release ID 和最终构建、验收身份一致。最终两份持久 authority 经完整性校验重载后与报告快照一致，均无未消费票据，pending 为 consumed；仓库和 worktree 各有 7 对 started/outcome，按 authorization ID 一一匹配。验收后的只读 WSL 检查确认没有残留 controller、FUSE 或 binfmt 临时目录。

最终构建第一次给 WSL 传入 Windows 回环代理 `127.0.0.1:7897`，apt 明确报 `Connection refused`；WSL 该端口没有代理监听。取消这个显式代理参数后，固定 snapshot 更新、固定软件包安装及环境完整性/健康检查全部通过。只调整本次构建参数，没有更改软件源、固定版本或沙箱网络策略。

## 完成审计

| 要求 | 实现与验收证据 |
| --- | --- |
| 范围内自动授权，范围外等待可信决策 | `PermissionScope` / `authorize_action`；专项测试断言范围内不调用审批 callback，真实 repo/worktree 验证 read/pytest 自动放行、write/install 暂停 |
| 精确调用、输入版本和运行身份绑定 | `request_action` / backend `permission_inputs`、`consume`；专项测试覆盖 timeout、owner、generation、协调 revision 改变，实机覆盖 argv、源码和 wheel 改变后拒绝 |
| 决策本身不执行、不提前扩权、一次性消费 | `resolve` / `consume_action`；实机 pending/reload/approve 阶段均无写入，消费后才提交 grant；专项测试拒绝票据和 call ID 重放 |
| 拒绝、撤销、取消不 dispatch | `replace_scope` / `cancel_pending` / routing；实机拒绝/取消/撤销及输入改变用例，专项测试验证 backend calls 为空和资源注册失败销毁 ticket |
| 任务外持久状态与重载 | `PermissionService.for_binding` 和受信 state root；实机两份 authority 与重新读取的持久状态完全一致 |
| canonical 可信工具和统一审计 | `sandbox_tool_access` / `execute_canonical_tool`；registry handler/tier/terminal 注入测试，实机 trusted data 及 approval/denial/start/outcome 配对 |
| 审批暂停与原调用、原 attempt 恢复 | AgentLoop / checkpoint / PlanSession / recovery；专项测试验证 XML/native 路径、延后调用不覆盖审批 checkpoint、allow/deny 恢复和 Plan attempt ID 不变 |
| 审批不改变 OS 隔离 | WSL2/bwrap 固定 profile；实机 repo/worktree 隔离断言通过，shell/网络安装仍拒绝，成功结果需要已消费 permission evidence |

未运行全仓测试，未提交、推送或创建 PR；原有工作区改动保留。

## 边界

- approval 只授予精确 sealed invocation 的 scope，不改变 WSL/bwrap OS profile，不授予 host shell、网络或 Docker socket。
- trusted data/control 工具只能来自固定 canonical allowlist，并在沙箱外执行；其权限仍由同一 authority 审计。
- 跨 owner 的完整恢复和 sandbox task 资源淘汰属于 S6；S5 只验证当前 authority 与当前 generation 的恢复栅栏。
