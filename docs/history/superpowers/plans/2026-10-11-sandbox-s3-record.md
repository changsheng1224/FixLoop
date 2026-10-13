> 历史设计／实施记录，仅供人工查看。当前流程见 docs/REPAIR_CONTRACT.md。

# S3：统一工作区工具执行

日期：2026-10-11。分支：`codex/sandbox-s3-workspace`。

## 范围

Windows 工作区使用专用 WSL2、bwrap 和 FUSE 受保护视图。工作区仍为用户真实仓库或真实 Git worktree，不复制仓库、不直接挂载原始 DrvFS。源码读取、搜索、Python AST、补丁计算与语法检查在固定 worker 内执行；控制面、权限判断、edit lock、任务状态和 receipt 留在隔离边界外。

宿主的狭窄 broker 仅提供受保护的文件读取和按版本提交 inode replacement。现有 inode 不原地写入；worker 生成的 mutation plan 不表示写入成功，只有 broker 完成提交及持久 receipt 才表示成功。多个文件的提交不承诺跨文件原子性；失败时报告已提交路径及恢复文件，无法确认时冻结该工作区执行。

## 实现

| 能力 | 实现与证据 |
| --- | --- |
| 工作区读取 | `read_file`、`list_files`、`grep`、`ast_parse`、`inspect_file`、`find_test` 接入固定 worker；只使用 Python 搜索，不执行项目提供的二进制或导入项目代码 |
| 编辑 | `write_file`、`patch_file`、`apply_patch` 在 worker 生成有界 mutation plan；broker 校验路径、版本和 preimage 后提交 |
| 最小依赖 | 补丁 hunk 规范化放在标准库 `patch_engine` 中，由宿主与 worker 共用，worker 不导入宿主工具注册模块或依赖其 `jsonschema` |
| 敏感文件与别名 | 阻止 `.git`、`.env`、凭据路径、父目录逃逸、reparse point 和多链接文件；以 Windows 原生句柄锚定目录和校验文件身份 |
| 读取一致性 | 每次调用固定读句柄并拒绝并发写入，读取完成后释放；写入时重新验证此前读取获得的 SHA256 |
| 路由与授权 | Layer 1 / Layer 2 注册工具及 `run_with_context` 均接入沙箱；未知或未迁移工具拒绝，缺失 backend 或不确定状态在宿主路径解析和快照之前拒绝 |
| 上下文证据 | observation recording、批量读取 freshness、上下文源码 freshness 使用沙箱证据，不在宿主重新读取或 hash 源码 |
| 批量调用 | shallow-copy contexts 共享 dispatch lock、版本锁及 uncertainty Event；通过实际 AgentLoop native batch 测试，结果与 provider call ID 保持配对 |
| 收尾 | 控制器 stdin 独立写线程、有界输出后继续 drain、worker kill/wait 和管道收尾；broker 接收线程使用可停止的原始 fd 分帧读取，终态前确认线程退出；backend 有界取消与延迟 broker 释放，无法确认收尾则报告 uncertain |
| 启动身份 | 固定系统 `wsl.exe` 与产品控制器路径；先以专用用户测量真实发行版的 root 所有权和内容，再在任何线程启动前进入私有 user/mount namespace，禁用挂载传播 |
| 协调资源 | backend 异常时资源转为 unknown / cleanup unverified，不掩盖原始异常；排队取消返回 cancelled |

## 验证状态

S3 实机验收已通过：`2026-10-11-sandbox-s3-probe.json` 的 `status` 为 `passed`，67 项检查全部为 true，记录了 38 次工具调用。环境构建记录 `2026-10-11-sandbox-s3-environment.json` 的 release ID、distribution 和 runtime digest 与验收记录一致：

- release ID：`dea853c023a65fb6ad0e9095b0795b915a2d40ddc38bc7f5ae8eca60a2dfb790`
- distribution：`FixLoop-Sandbox-s3-v1-dea853c023a6`
- runtime digest：`81f954bcbf1eec6c86a9dc9f3616360d0c3068a013be3ff9bae2acbf39143f98`

实机证据覆盖实际 FixLoop 仓库只读访问，以及独立真实仓库和 Git worktree 的读取、搜索、Python AST、patch、条件新建和 apply_patch 提交。敏感路径、父目录、hardlink 与 junction 被拒绝；过期写入保留用户并发修改；持久 receipt 确认提交和收尾。实际 worker 的宿主目录隐藏、网络隔离、只读挂载、嵌套 user namespace 拒绝与 broker fd 隔离均通过。

取消在真实 FUSE RPC 开始后触发，执行有界结束，返回 `cancelled` 且 cleanup 为 `confirmed`。验收结束时，专用发行版的临时 FUSE / binfmt 目录和运行中的 workspace controller 均为空。生产门禁仍关闭，此次通过只表示 S3 范围验收完成。

相关测试已按影响范围分批运行：contract / worker / broker 前轮 100 passed；相关既有回归 125 passed；本轮边界测试首批 28 passed / 2 failed，测试修正后仅复跑两个失败用例和恢复的 edit-lock 用例，3 passed。新增 native sandbox batch、broker cleanup 异常和执行层统计回归已通过；未运行全仓测试。此前通过结果与后续直接影响用例分别保留，不将这些批次数相加成独立用例总数。

实机排查已发现并修正：包管理代理需使用实际 WSL 网关；Agent 对首次出现的执行层统计字典初始化顺序错误；S3 bwrap 启动必须显式传 `--unshare-user` 才能配合 `--disable-userns`；固定工作区 worker 使用与最小环境匹配的动态库清单，避免引用旧 P1 的未安装库；完整性测量必须先于 namespace 所有者映射，否则真实 root 会映射成 overflow UID 65534；控制器不能在 buffered stdin 接收线程仍阻塞时退出，否则 Python 3.14 在解释器收尾时发生致命错误。

验收脚本也修正了 `/proc/self/fd` 枚举时把枚举自身的临时描述符计为泄漏的问题，使用 `readlink` 确认仍存活的描述符，并记录实际描述符目标。显式验收额外保存控制器 stderr，生产 transport 仍不暴露诊断内容。

编辑实机验收发现 worker 复用 hunk 函数时导入了宿主 `tools`，间接要求 `jsonschema`。函数已移到纯标准库 `patch_engine` 并在两侧共用；新增 `python -I -S` 子进程验证三种编辑模式均能独立生成 plan。对应宿主 apply_patch 回归 10 passed，直接受影响 worker 编辑回归 8 passed。接收线程停止与分帧、namespace 顺序和映射失败、固定启动器等新增回归已分别通过。

## 阶段限制

生产沙箱门禁继续关闭。当前是 S3 的受保护工作区工具接入，不代表 S4–S7 完成：generic shell / pytest 命令、Git 工具、snapshot/diff/rollback、审批升级、完整生命周期和 CLI 默认启用仍在后续阶段；LSP 和 Java AST 尚未迁移，沙箱模式下拒绝。

源码 freshness budget 控制读取字节和 worker 计算，并在返回后重新检查 deadline；环境测量、启动与收尾仍有额外 allowance，不承诺整个调用的墙钟耗时严格等于 freshness budget。超预算证据不进入上下文。

旧 S0/S1/S2 环境和此次调试环境保留；环境版本淘汰属于后续生命周期阶段。未提交、推送或创建 PR。
