# S2：运行入口与工作区隔离

MVP 范围：本地单用户、多项目、并发修复。工作区策略解决“编辑哪份代码”，执行后端解决“仓库代码能访问什么”。两者独立选择，不再读取 `FIXLOOP_USE_WORKTREE` 决定 repair 行为。

| 入口 | 工作区策略 | 执行后端 | 边界 |
| --- | --- | --- | --- |
| repair CLI、标准 factory 默认 | `worktree` | `legacy` | 独立代码副本；宿主工具不构成安全沙箱 |
| 推荐隔离组合 | `worktree` | `wsl_bwrap` | 独立副本 + 任务绑定的文件与命令沙箱 |
| 显式指定目录 | `provided` | 调用方选择 | 允许修改指定目录；副本与并发写入责任由调用方承担 |
| Workbench/API repair | `provided` | 生产路径 `wsl_bwrap` | Workbench 已物化独立运行副本，不再创建第二层 worktree |
| 评测 harness | `provided` | 现有验证配置 | harness 已为 case/attempt 准备目录 |

CLI 示例：

```powershell
# 干净 Git 根目录，独立 worktree + WSL/bwrap；初始化失败停止
python -m src.cli repair --repo C:/projects/demo --issue "修复失败测试" --execution-backend wsl_bwrap

# 显式允许直接修改目录；适用于脏仓库、非 Git 目录或调用方已有副本
python -m src.cli repair --repo C:/projects/demo --issue "修复失败测试" --workspace-policy provided --execution-tier host
```

标准 Python API 使用 `make_orchestrator_factory(workspace_policy=..., execution_backend=..., state_root=..., project_root=...)`，或相同参数的 `wire_orchestrator()`。低层 `Orchestrator` 只消费已装配的 Agent，默认 `provided`；需要它创建副本时显式选择 `worktree`。创建托管 worktree 时，输入必须是源项目 Git 根目录；`project_root` 不能指向另一个项目。旧 `sandbox_worktree` 参数已移除。宿主修复 Agent 固定在指定目录，源码修改或进程 cwd 变化不会把工具重定向到其他目录。

## 准备顺序

1. 校验输入及后端配置；WSL 环境初始化不可用时抛错，禁止自动切换宿主执行。
2. 确定 run ID，创建或验证托管 worktree；源目录有修改、非 Git、路径/分支冲突时停止，不切换为 `provided`。
3. 把最终 workspace 绑定到 Agent、工具上下文、会话身份和 prompt；WSL 后端使用不可变 sandbox binding。
4. 调用交付 workspace 回调，然后校验恢复 checkpoint；快照、协调 owner、trace 和修复均使用最终目录。
5. 结束时保留 checkout、候选修改和 ownership lease，方便查看和恢复。Agent 不再切回源目录。

本地 `worktree` 默认交付目录位于共享外部状态根的 `projects/<project_id>/deliveries/<id>`。显式 `--output` 也必须位于源仓库外，避免报告文件影响源仓库状态。托管 worktree 与运行状态分别在项目下的 `worktrees/<run_id>`、`tasks/<run_id>`；不同 run 不复用可写目录。Workbench 的 `provided` 状态根仍由服务管理。

## 恢复与失败

checkpoint 的 `node_timings.run_entry` 保存工作区策略、执行配置、沙箱策略、源项目与执行目录的路径和目录实体身份。恢复必须完全一致；缺失记录或策略改变进入 `recovery_required`，不当作新任务执行。WSL 还必须匹配原 sandbox binding（环境、策略摘要、项目和工作区身份），策略参与摘要。

宿主 worktree 恢复从源项目 + run ID 定位原 checkout，通过 ownership lease、Git common directory、分支及目录实体检查后复用；禁止重新创建副本。WSL 复用原 binding 中的 checkout。CLI 恢复应传入原 `--repo` 输入、原工作区策略和执行配置，以及 `--resume-repair <run_id>`，交付报告使用新目录。

初始化失败保留已创建的可疑资源与诊断，修复不进入源目录执行。不自动迁移旧 checkpoint；S2 之前缺少入口记录的 checkpoint 需要人工检查。

## 面试说明与限制

“我把工作区与执行权限分成两个显式策略。可信入口确定策略，控制面先准备最终目录，再绑定 Agent 和恢复记录。并发任务各自编辑副本；WSL/bwrap 限制仓库代码访问宿主。创建失败停止，恢复校验原目录实体、运行身份和策略，结束后保留候选补丁。”

`worktree` 共享 Git 管理区，不等于操作系统沙箱；`legacy` 保留现有 Docker/host/static 验证选择，其中 Docker 验证不能隔离其他宿主工具。`provided` 本身不承诺独立副本，Workbench 的隔离来自其运行副本机制。这里不实现多用户鉴权、缓存隔离或记忆访问边界。

验证使用真实临时 Git 仓库、并发运行、确定性模型输出和现有 WSL 测试后端；未要求在线模型或真实 WSL 演示。实际 WSL 环境仍由已有初始化与执行门禁检查。
