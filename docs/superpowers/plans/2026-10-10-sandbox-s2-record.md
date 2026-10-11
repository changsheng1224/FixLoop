# S2：专用 WSL2 运行环境准备

日期：2026-10-10。分支：`codex/sandbox-s2-environment`。

## 范围与结果

S2 实现 Windows 上的专用运行环境自动准备：检测 WSL、导入产品自有 WSL2 发行版、安装固定执行器和基础工具链、检查版本及完整性、执行健康探测、从安装中断继续，并提供离线镜像打包入口。现有 WSL2 环境中，从官方 Ubuntu base 镜像自动准备及镜像导出已经实跑成功。

该发行版按执行器发布版本共享；会话与工作区的绑定沿用 S1 的任务上下文。S2 没有启用生产工具执行，环境健康通过不能代替工作区隔离验收。Windows 生产沙箱门禁继续关闭，后续 S3–S7 仍需实现工作区映射、工具接入、审批和完整生命周期。

## 实现与验收证据

| 要求 | 实现 | 验证 |
| --- | --- | --- |
| 检测 WSL2 | 固定参数调用 `wsl --status` 和发行版清单；注册路径及版本必须匹配自己的安装记录 | 当前 WSL 3.0.1.0；实际专用发行版为 WSL2；测试覆盖 WSL 缺失、系统组件未启用、WSL1 和名称冲突 |
| 不修改用户发行版 | 明确指定产品名称与用户；仅在对应专用发行版安装和配置，不操作默认发行版 | 用户 Ubuntu 的 `/var/lib/dpkg/status`、`/etc/wsl.conf` 摘要前后一致 |
| 固定基础环境 | Ubuntu base 26.04.1 的 HTTPS 下载及 SHA256 校验；2026-10-09 固定 Ubuntu 仓库快照；Ubuntu archive keyring 验签；Python、bwrap 和 wheel 精确版本 | 实机：Python 3.14.4、pytest 9.1.1、bubblewrap 0.11.1 |
| HTTPS 引导 | 基础镜像无 CA 包时，从已校验的固定 certifi wheel 提取证书，显式指定 APT CA 路径；更新索引失败即停止 | 干净基础镜像实跑成功；没有关闭 TLS 验证或允许未认证包 |
| 固定执行器 | 仅收集 `agent_runtime` 的 Python 源码；用固定 Python argv 和 stdin 安装校验过的 payload | 控制器依赖代码包含在 payload 中；不打包仓库凭据、用户目录或任务状态 |
| 版本及完整性 | release ID 覆盖配方、执行器和 wheel 摘要；运行时测量覆盖代码、venv、标准库、动态库、loader 链接、配置、账户信息、dpkg 状态及权限 | 实机修改执行器后拒绝使用且不重装；恢复原字节后摘要一致 |
| 健康检查 | `fixloop` UID 1000 实际运行 bwrap namespace、网络、tmpfs、环境变量与路径探测；验证 `/dev/fuse` 可读写打开 | 六项实机检查全为 `true`；同一端口在隔离外先证明可连接，再验证隔离内不可连接 |
| 安装中断恢复 | 内容寻址下载缓存、HTTP Range 续传、OS 安装锁、校验过的阶段 journal；核对注册路径后继续；未确认的残留导入目录保留 | 实际导入完成后模拟中断，journal 停在 `importing`，再次准备没有重新导入；其他阶段与下载断开由相关测试覆盖 |
| 离线打包与导入 | 打包脚本导出 `tar.gz` 和可信发布 sidecar；导入前检查发布 ID、镜像 SHA256，之后核对运行时摘要和健康状态 | 实际导出后从该镜像重新导入；未下载、未运行 APT，运行时摘要与在线准备完全一致 |
| 就绪环境复查 | 只重新核对注册信息、完整性与健康状态 | 在线就绪及离线导入后的普通复查均不再次导入或安装 |

系统缺失时提供 `wsl --install --no-distribution`、管理员权限、虚拟化和重启指引；不自动提升权限。本次没有禁用 Windows 系统功能来实测缺失场景，该部分证据来自相关测试。

## 模块

- `agent_runtime/linux_sandbox/environment.py`：发布配方、缓存、安装状态机、注册核对、锁、完整性与健康门禁。
- `agent_runtime/linux_sandbox/environment_worker.py`：专用发行版内的可信安装、测量及健康执行器。
- `agent_runtime/linux_sandbox/environment_release.json`：固定 rootfs、仓库快照、Debian 包及 wheel 配方。
- `agent_runtime/linux_sandbox/wsl_launcher.py`：从准备结果构造专用发行版 launcher，要求任务用户为 `fixloop`；保留已有显式 Ubuntu 契约。
- `scripts/build_sandbox_environment.py`：准备、诊断、导出和离线导入入口。
- `pyproject.toml`：发布包包含配方 JSON。

默认安装及缓存位于 `%LOCALAPPDATA%/FixLoop/sandbox`，不得落在用户工作区内。`wsl.conf` 仅写入专用发行版：默认非特权用户、禁用 Windows interop、Windows PATH 注入和 Windows 驱动器自动挂载。环境准备属于可信控制面操作，不接受 Agent 生成的宿主命令。

## 复现

```powershell
python scripts/build_sandbox_environment.py --workspace C:/Users/haoyu/Documents/FixLoop
python scripts/build_sandbox_environment.py --workspace C:/Users/haoyu/Documents/FixLoop --export C:/path/to/new-release-directory
python scripts/build_sandbox_environment.py --workspace C:/Users/haoyu/Documents/FixLoop --bundle C:/path/to/trusted-release/bundle.json
```

首次在线构建需要访问固定 rootfs、PyPI 和 Ubuntu snapshot。下载支持标准进程代理环境变量，APT 支持可选 `--package-proxy`；没有把本机代理地址写进产品。发布 sidecar 必须来自可信安装包或自己的构建流程，SHA256 校验不负责为任意外部 sidecar 建立信任。

发行版名称由版本及 release ID 确定。名称已被别的安装路径占用时会拒绝使用；`--home` 不能静默接管同名环境。打包入口用于仅包含运行环境的发布构建；本阶段没有向该发行版挂载或执行用户项目。

## 实机产物

- 在线准备及导出：`2026-10-10-sandbox-s2-probe.json`。
- 真实导入边界中断：`2026-10-10-sandbox-s2-interruption.json`。该记录对应修复健康参数前的执行器版本；恢复行为已实际验证。
- 离线重建及就绪复查：`2026-10-10-sandbox-s2-offline.json`。
- 完整性拒绝与恢复、用户 Ubuntu 摘要核对：`2026-10-10-sandbox-s2-verification.json`。

最终发布 ID：`64765fbcba7f3e2b0fb1fff2fb3115d122a92bd41d8f1365cf2fa5096a5bcdbb`。

运行时摘要：`4027177d6e3596b8da5e6e8699334cde04573f5ff60c6dfaca955e6555e02a0c`。

镜像摘要：`86c4f7dcb2477bc629c3d3fd97ca31b17b676ddfadb0486159c111bb85cf4285`。

离线镜像保存在 `%LOCALAPPDATA%/FixLoop/sandbox/releases/s2-v1-20261010`，未放进仓库。

## 相关验证

`tests/test_sandbox_environment.py` 的 44 个用例与原有 `tests/test_wsl_launcher.py` 的 7 个用例已按改动影响范围分批通过，共 51 个用例。新增失败用例仅复跑失败或直接受影响的用例；未运行全仓测试。S2 文件的 Ruff lint、format 检查和对应 tracked diff 的空白检查通过。

实机调试解决了三个环境问题：滚动仓库移除旧版本、裸镜像没有 CA 引导材料、bwrap 要求显式 `--unshare-user`。完整性校验还拒绝带有 `..` 路径逃逸的安装记录，即使其 checksum 已重新计算。

早期调试生成的版本环境和安装记录保留，避免自动覆盖或删除旧安装；生产版本淘汰与清理属于后续生命周期阶段。原有 S0/S1 和其他未提交改动保持保留；没有提交、推送或创建 PR。
