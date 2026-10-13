> 历史设计／实施记录，仅供人工查看。当前流程见 docs/REPAIR_CONTRACT.md。

# S4：命令、依赖与验证

日期：2026-10-11。分支：`codex/sandbox-s4-commands`。

## 实现范围

沿用 S3 的 Windows 原生 broker、专用 WSL2、bwrap 和 FUSE 受保护工作区视图。工作区为用户真实仓库或 Git worktree，不复制仓库、不直挂 DrvFS。控制面与 receipt 留在边界外，Python、pytest、离线 pip 和内部源码消费在沙箱内执行；沙箱失败不回退到宿主执行验证或读取源码。

| 能力 | 实现 |
| --- | --- |
| 命令 | 固定 Python profile，支持 `-c`、脚本、pytest 和本地 wheel 安装；`run_shell` 仅映射受支持的 Python argv，拒绝通用 shell、网络安装和源码构建 |
| 隔离 | 命令不挂载产品 executor，不继承 broker 通道或任务 lease；只读 toolchain / 源码，私有 `/tmp`，禁网、清空继承环境、禁嵌套 user namespace |
| 任务依赖 | WSL 原生任务目录由 workspace / task / run / runtime 身份派生；跨调用保留，任务 lease 串行保护，普通命令只读、离线安装可写 |
| 离线安装 | 显式本地 wheel，固定 `--isolated --no-index --no-deps --only-binary=:all: --no-compile --no-cache-dir --target`；不执行 `.pth` 或 site hooks |
| 源码版本 | 沙箱内 SHA256 inventory 包含二进制；未计入版本的缓存和虚拟环境目录在命令视图中隐藏，避免读取旧 pyc 绕过源码指纹 |
| 快照与回滚 | 内部分页文本快照附完整版本；CAS 恢复仅允许任务自己提交的当前版本，拒绝覆盖用户后续编辑；新建文件可按归属删除，部分提交保留成功路径及版本 |
| 预览与差异 | 编辑预览及 preimage / syntax check 走固定 worker；执行器采用 broker receipt 的实际提交差异，不在宿主重新读取 before / after 源码 |
| 验证 | 固定 pytest profile，禁插件自动加载与 cacheprovider，临时目录 `/tmp/pytest`；记录测试计数，环境失败或没有测试不能产生通过证据 |
| 证据有效性 | 验证绑定 source version、dependency version、task environment 和 runtime digest；前后扫描不一致不产生 current 证据，任一绑定变化使旧验证失效 |
| 内部消费 | PlanSession 的快照、恢复、重规划和 evidence provider 接入沙箱；Orchestrator 的快照、回滚、测试目标选择和失败源码摘要接入相同路径 |
| 超时与取消 | 环境前扫描、命令和后扫描共享命令预算；输出有界保留并持续 drain，项目 stdout 不解析为控制协议；确认清理的超时不会误冻结执行器 |

## 验证

最终实机验收通过：`2026-10-11-sandbox-s4-probe.json` 的 `status` 为 `passed`，63 项检查全部为 true。构建记录 `2026-10-11-sandbox-s4-environment.json` 与验收记录的发行版、release ID 和 runtime digest 一致：

- release ID：`ab93f15e3ceadef2c3d5107330218f0ae6ade762fb35b68308a9d8ad7fc2530a`
- distribution：`FixLoop-Sandbox-s4-v1-ab93f15e3cea`
- runtime digest：`ae92e7e52fef08c42365593fd454ae0912dfb79fdeecc4c4bcddb785e719f7f1`

真实仓库及 worktree 分别完成 Python 读取、离线 wheel 安装、跨调用 import、修复前 pytest 失败、CAS 补丁后通过、同任务依赖身份、只读源码 / 可写临时目录、CAS 回滚、用户外部修改保护、二进制版本及另一任务依赖隔离。生产 EvidenceLedger 在实机检查当前验证有效性，并确认源码回滚和安装额外依赖后旧证据失效。命令视图隐藏未计入版本的缓存。

隔离检查确认宿主路径及控制器通道不可见、WSL interop 环境不存在、无网络路由且连接被拒绝、源码和依赖只读、临时目录可写、嵌套 user namespace 被拒绝、没有 broker 描述符。stdout 伪造不能提交修改或伪造终态，200 kB 双流输出有界保留并完整 drain；包含 fork 子进程的超时及取消均确认清理，后续调用正常，取消 receipt 已持久化。验收结束时，临时 FUSE / binfmt 目录和运行中的 workspace controller 均为空。

相关测试按影响范围分批运行，保留未受后续修改影响的通过结果：Plan / verifier / tools / executor 回归 173 passed；S4 首批与 controller / backend / worker 回归 58 passed；snapshot / rollback / verify-loop / environment 回归 65 passed。源码视图及失败反馈批次 135 passed、3 failed，定位后只复跑三个失败用例，3 passed。最后命令路由与 controller 回归 33 passed；新增 executor 超时清理测试和 CA 挂载直接影响用例分别通过。各批次有重叠，不相加为独立用例总数。未运行全仓测试。

实机首次发现 Ubuntu pip 即使离线安装也会加载固定 CA 文件。实际路径为 `/etc/ssl/certs/ca-certificates.crt`，该文件已有环境完整性测量；补入只读挂载，未开放网络或放宽 TLS。修正已打包到新的发行版，没有直接修改已验收发行版内容绕过完整性校验。

fd 验收最初将所有额外描述符视为泄漏。读取实际目标与访问标志后确认，仅存在指向固定 `libffi.so.8` 的只读运行库句柄；检查与 S3 契约保持一致，仅允许该精确只读目标及标准输入输出。验收脚本增加按同一 release 恢复隔离阶段的选项，保留已通过的仓库 / worktree 检查，仅复验受影响阶段和未完成部分；原失败调用仍保留在记录中。最终相关 lint 全部通过，28 个修改文件格式检查通过。

## MVP 限制

- Python 3.14 / pytest 为发行版固定版本；仅支持 Python 验证。
- 命令看到只读源码。需要向源码树写文件的测试会失败；临时文件应使用 `/tmp` / pytest `tmp_path`，源码编辑仍经 S3 CAS 工具。
- 依赖只支持显式离线 wheel，不自动解析 requirements，不安装 sdist，不支持源码构建、联网解析或自动补依赖。安装失败可能留下部分依赖，以新的 dependency fingerprint 为准，不承诺安装事务性。
- inventory 最多 4096 个文件、256 MiB；依赖最多 8192 个文件、256 MiB。文本快照单文件最多 2 MiB，编码页预算约 600 kB；超限明确失败，无宿主回退。
- 二进制参与版本与变化检查，不提供二进制快照恢复；筛选快照排除的内容也不能恢复。
- 源码与依赖哈希不包含未挂载的缓存 / 虚拟环境；快照版本仅表示可执行源码视图。并发修改导致扫描或 CAS 失败，不覆盖外部修改；多文件恢复不保证跨文件原子性。
- 回滚归属当前保存在 ToolContext；跨进程恢复其持久归属、任务依赖回收和发行版淘汰仍属于后续生命周期工作。源码指纹采用内容版本，不把同内容写回视为不同版本。
- 本轮生产沙箱门禁保持关闭，不代表后续审批升级、完整生命周期或默认启用已完成。实机验收使用独立真实仓库与 worktree，不将它宣称为整个 FixLoop 大仓库的命令验收。

未提交、推送或创建 PR；原有工作区改动保留。

## 接续核验

接续时确认 S4 实机报告仍为 `passed`，63 项检查无失败，构建与验收身份一致，清理记录没有残留 controller 或临时挂载。摘要提到的网络安装拒绝断言属于 S5 验收，不修改或重跑已通过的 S4 实机报告。

当前工作区包含后续权限改动，因此补跑 `tests/test_sandbox_s4.py`，33 passed；命令 profile、任务依赖、inventory、工作区状态、controller 和 S4 测试共 6 个文件的 lint 与格式检查通过。默认 Python 3.14 缺少 `jsonschema`，首次调用未开始收集测试，改用已有 Anaconda Python 3.13.9 完成核验。pytest 报告一条缓存目录写入权限 warning，不影响用例结果；未安装依赖、未运行全仓测试。
