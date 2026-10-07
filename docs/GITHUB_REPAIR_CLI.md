# GitHub 修复 CLI

## 用户流程

配置一次 API、Git 认证/代理和 Docker 验证环境，然后输入仓库与问题：

```bash
fixloop repair --repo owner/project --issue "输入、实际行为与预期行为"
```

CLI 按 preflight → clone → snapshot → repair → delivery 输出进度，调用已有
Patcher / Critic / Verifier。每次远程修复使用新目录和 detached HEAD；不复用、清理
或重置用户的其他仓库，也不自动 push 或创建 PR。

## 输入与运行目录

- `--repo`：现有本地目录、GitHub HTTPS URL、GitHub SSH URL、`owner/repo`。
- `--issue` / `--issue-file`：二选一。文件必须是非空 UTF-8 文本，支持 BOM。
- `--ref`：远程分支、tag 或 SHA；省略时固定远程默认分支当前 HEAD。
- `--output`：新的结果目录；省略时 `.fixloop/runs/<uuid>`。
- 本地目录的输出放在仓库外或 `.fixloop` 下，避免报告成为修复上下文。

API 配置来自调用目录的 `.env` 或已有环境变量，CLI 不切换到远程仓库读取其 `.env`。
拒绝嵌入凭据、额外路径、query 和 fragment 的 GitHub URL。
Git 禁用交互认证提示；需提前配置 SSH key 或 credential helper。

远程输入首期支持 Python/pytest。默认 `auto` 转为 `container`，容器不可用时
不自动降级；显式 `host`、`static`、`--skip-verify` 仍可选择其他行为。
容器仅表示独立验证后端，整个 Patcher 运行时没有因此获得容器隔离。
项目依赖准备复用已有验证能力；缺失依赖记录环境失败，不对任意仓库自动联网安装。

## 交付契约

`result.json` 与 `report.md` 在准备阶段即创建，每个阶段更新；失败和取消也会保存。
结果记录仓库、工作目录、基线 commit、runtime run_id、验证统计、进度和错误分类。

- `fixed`：运行时判定修复，且有非零测试通过证据。
- `pending_verify`：运行时有补丁，但没有测试通过证据（跳过、静态或零测试）。
- 其他状态：保留运行时 failed / exhausted / regression / timeout /
  user_cancel / recovery_required 语义。

成功仅覆盖运行时实际选择的测试范围。原有进程成功码表示补丁可以交付，
不等于验证通过；自动化调用必须同时读取状态和验证字段。

`patch.diff` 比较启动与最终工作树，使用独立临时 Git index，包含新增文件、删除和
二进制修改；不写入用户实际 index。启动前已存在的未跟踪文件不进入本地导出。
运行产物、环境文件和常见缓存排除。没有变化时补丁文件为空且 `patch_available=false`。
非 Git 目录使用文本快照，二进制修改会明确报告导出失败。

应用到报告中的基线工作树后先运行 `git apply --check`。本地原地修复的基线可能
包含用户尚未提交的修改，此时应应用到相同的启动前工作树。

## 失败与取消

常见错误分类：input_invalid、configuration_failed、clone_failed、
authentication_failed、reference_not_found、unsupported_project、
verification_environment_failed、verification_failed、runtime_failed、no_changes、timeout、user_cancel。
私有仓库不可访问与仓库不存在可能产生同一种 Git 错误，认证分类提示用户同时检查地址与权限。

克隆限时 300 秒；普通 Git 操作限时 120 秒。取消保留已有目录与诊断，
修复阶段取消或异常后尝试导出实际剩余修改，不把中间补丁标记为修复已验证。
已有输出目录会被拒绝，不覆盖上一次任务。

首期远程输入不直接续跑；已有 `--resume-repair` 可配合保存下来的本地
`--repo <output>/repo` 使用。通用恢复与依赖准备属于后续工作。

## 实现验证（2026-10-01）

按影响范围分组执行及精确复验，累计 90 个用例通过、1 个既有用例跳过。
覆盖仓库解析、默认分支/分支/tag/SHA、版本不存在、实际 Git 克隆、补丁应用检查、
新增/删除/二进制文件、CRLF、用户 index 保留、已有暂存删除、取消、失败报告、
零实际修改、dry-run，以及 CLI repair/exit/eval、WSL 入口门控和 skills 子命令。
修复运行时使用注入的 fake；没有调用真实 API 或 Docker 做端到端修复。

改动 Python 文件的 Ruff lint、格式检查与 `git diff --check` 通过。
整体 `ruff check agent_runtime src tests` 报告 23 个既有问题；相关文件与分支基线
一致，本次没有改动。未执行全量测试或发布级验证。

wheel 构建、Layer 2 源码/提示/Skill 资源、缓存排除、隔离安装和 CLI 帮助入口已验证。

## 修复链路验收（2026-10-07）

新增 `tests/test_github_repair_chain.py`，直接调用公开 CLI 和真实 runtime 工厂。
使用独立合成 Python 项目，原有测试先失败，Agent 经文件工具修改源码，再由真实
pytest 验证；交付补丁重新应用到同 SHA 的干净克隆后再次执行测试。断言覆盖固定
基线、run_id、阶段进度、trace、实际验证后端、完成回执、原有测试不变和补丁范围。
错误补丁也必须留下失败证据，不能标记为 `fixed`。

各层外部依赖边界如下：

| 验收层 | 实际执行 | 受控部分 |
| --- | --- | --- |
| XML / native CLI 回归 | parser、Git clone/checkout、工厂、Agent、文件工具、宿主 pytest、补丁交付 | 模型响应；Git URL 通过进程环境重写到本地 fixture |
| 默认 Docker CLI | 同上，且默认选择真实 Docker pytest，确认容器删除后再交付 | 模型响应、本地 Git fixture |
| GitHub 联网 smoke | GitHub HTTPS 拉取、指定 SHA、detached HEAD、干净工作树 | 不执行 Agent 修复 |
| 真实模型验收 | 实际模型 provider、CLI、Agent 工具、Docker 验证和补丁重新应用 | 使用本地合成 Git fixture |

这几层组合覆盖入口与外部依赖；没有在同一次运行里串联真实 GitHub 仓库和真实
模型修复。公开仓库 HTTPS smoke 使用 Git，无需 GitHub MCP 或 PAT；私有仓库仍需
预先配置 Git 认证。GitHub MCP 的工具联网测试另行维护。

离线回归与 Docker 回执测试：

```powershell
python -m pytest tests/test_github_repair_chain.py tests/test_docker_verification_receipt.py -v --basetemp=.runtime-tools/p-chain
```

默认跳过需主动启用的外部依赖验收。启用后缺少环境会失败，不会静默跳过：

```powershell
$env:FIXLOOP_CHAIN_DOCKER = '1'
python -m pytest tests/test_github_repair_chain.py::test_public_cli_default_docker_verifies_real_patch -v --basetemp=.runtime-tools/p-docker

$env:FIXLOOP_CHAIN_GITHUB = '1'
$env:FIXLOOP_CHAIN_GITHUB_REPO = 'https://github.com/pypa/sampleproject'
$env:FIXLOOP_CHAIN_GITHUB_SHA = '621e4974ca25ce531773def586ba3ed8e736b3fc'
python -m pytest tests/test_github_repair_chain.py::test_live_github_clone_pins_requested_commit -v --basetemp=.runtime-tools/p-github

# 复用调用目录 .env 的模型配置；会产生实际 API 调用费用。
$env:FIXLOOP_CHAIN_MODEL = '1'
$env:FIXLOOP_CHAIN_DOCKER = '1'
python -m pytest tests/test_github_repair_chain.py::test_live_model_cli -v --basetemp=.runtime-tools/p-model
```

验收限制为 8 个工具步骤、8 次全局模型调用、90 秒修复期限及 30 秒工具期限。
规划与重新规划复用 Patcher 已配置的输出 token 上限，让推理模型有空间生成最终
JSON。Windows 使用较短的 `--basetemp`，避免深层运行产物路径超过系统限制。

`tests/test_docker_verification_receipt.py` 覆盖正常结束、测试失败、超时、取消、
删除失败，以及 Docker 自动删除与显式删除产生 409 的竞态。执行完成与容器删除
均确认后，验证回执才允许 `completed=true`。

本次按影响范围及精确复验累计 154 个不同用例通过，包含 21 个新增用例。真实
GitHub smoke、默认 Docker CLI 和真实模型 CLI 均已实际执行通过；模型验收使用
本地合成 Git 源，成功不代表任意 GitHub 项目的修复成功率。Ruff 全目录 lint、
格式检查和 `git diff --check` 通过。本次补测未重复执行全量测试。
