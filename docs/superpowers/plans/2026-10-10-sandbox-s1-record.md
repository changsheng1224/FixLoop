# 沙箱 S1：任务上下文与外部状态

日期：2026-10-10。分支：`codex/sandbox-s1-task-context`。

S1 已实现：先确定实际仓库或 worktree，再创建任务绑定和 backend；任务状态位于工作区之外；worktree 创建失败终止启动；保留用户修改和任务结果。此次没有推送、创建 PR 或合并。

## 需求、实现与验证

| 需求 | 实现 | 验证 |
| --- | --- | --- |
| 先确定工作区，再绑定沙箱 | Orchestrator 在 checkpoint、trace、snapshot 之前解析最终工作区；backend factory 接收不可变 `SandboxTaskBinding` | worktree 绑定顺序、真实 Agent 集成测试 |
| Windows / WSL / 沙箱路径映射 | `WorkspaceMapping` 保存规范主机路径、WSL 来源和 `/workspace`；记录目录身份及映射摘要 | 中文及空格路径、UNC/ADS/遍历拒绝、目录替换检测 |
| 固定任务与环境身份 | 绑定 task/run、环境版本和摘要、策略版本和摘要；Agent 不再随进程 cwd 切换；工具分派检查 context/backend/coordinator | context 漂移、backend 不匹配、协调器漂移、禁止重复绑定 |
| 可信状态不暴露给项目 | 外部任务目录承载历史、session、observation、plan、checkpoint、trace 和 collaboration 状态；拒绝项目内 state root | 外部状态集成、真实 Agent 与 Windows 长路径 blob 测试 |
| 恢复不改变任务身份 | 独占保存绑定；恢复检查 checksum、工作区目录身份、环境、策略与 checkpoint 绑定；采用保留的 task worktree | 绑定往返、损坏记录、checkpoint 不匹配、恢复复用 |
| worktree 失败不回退原仓库 | 新路径及原入口均向上抛出创建失败；已创建 checkout 在 backend 启动失败后保留 | 两条创建失败路径、启动失败保留 |
| 保留用户现有修改 | 原工作区可直接使用；脏源仓库拒绝创建基于 HEAD 的新 worktree；不覆盖已有分支、目录或 lease | 用户脏文件、用户 worktree、分支/目录冲突 |
| 明确 worktree 所有权与保留 | 外部 ownership lease 记录目录身份；正常结束、取消、启动失败均保留；只允许删除已验证的自有 checkout | 目录替换拒绝、脏 checkout 保留、Git 删除失败无 rmtree 回退 |
| 可信 Git 管理不运行项目代码 | 清除继承的 `GIT_*`；关闭 hooks、fsmonitor、submodule recursion 和 filter drivers；保留标量换行配置 | hooks/filter/继承 Git 环境测试、Windows 换行回归 |

## 外部存储与保留规则

Windows 默认根目录为 `%LOCALAPPDATA%/FixLoop/state/<repository-id>`；Linux 为 `$XDG_STATE_HOME/FixLoop/state/<repository-id>`，未设置时使用 `~/.local/state`。可通过 `SandboxTaskSpec.state_base` 或 `FIXLOOP_STATE_ROOT` 指定外部根目录。

```text
<external-base>/
  tasks/<task-id>/
    sandbox-task.json
    ...任务运行状态...
  worktrees/<run-id>/
  worktree-leases/<run-id>.json
```

运行状态沿用各存储组件的子目录布局。状态根目录必须在原仓库及最终工作区之外，不挂入 `/workspace`。Windows blob I/O 使用扩展路径，避免内容寻址文件名触发 MAX_PATH。

用户提供的仓库或已有 worktree 属于 `user`，任务不负责删除。FixLoop 创建的 checkout 属于 `task`，默认 `preserve`；任务结束、取消、启动失败只记录保留原因。清理仅处理明确标为 `discard_if_clean`、已 `released` 且过期的干净自有 checkout，不自动删除保留结果，也不删除分支。删除失败不会使用递归目录删除兜底。

脏源仓库请求创建新 worktree 时明确失败；用户可直接使用现有工作区。此次没有复制未提交修改、整个仓库或整个工作区。

## 验证记录

- 新增 `tests/test_sandbox_task_context.py`：46 个用例均已分批验证通过；最终收集确认 46 个用例。
- 修复后的失败用例及受影响 Git 删除检查：9 passed。
- 后续边界加固：15 passed；真实 Agent 用例发现 Windows 长路径问题，修复后该失败用例单独复验：1 passed。
- 剩余 S1 用例及相关状态、协议、工作区切换、历史、turn、协作、协调器、plan blob 回归：74 passed，20 deselected。
- 最终新增/受影响边界及既有 worktree、bwrap routing/verifier 检查：32 passed。
- 上述批次存在重叠，不能相加作为独立用例总数。已通过且未受后续修改影响的结果继续有效。
- 13 个受影响 Python 文件：`ruff check`、`ruff format --check` 均通过；`git diff --check` 通过。

未运行全量测试，未调用模型，未将这些单元/集成检查作为真实操作系统隔离验收。S0 的 guarded 实测证据及原有文件已保留。

## 接入边界与后续阶段

`sandbox_task_spec` / `sandbox_backend_factory` 是内部接入点；factory 返回的 backend 必须携带完全匹配的 `.task_binding`。本阶段不创建或安装 WSL 发行版，也不把 S0 的实验 broker 自动启用为生产 backend。

`windows_guarded` 仅表示映射要求，不能据此授权原始 DrvFS bind。绑定记录的 checksum 用于损坏检测，不是抗篡改认证。Agent 绑定时清除源仓库 warm evidence、workspace snapshots 和工作区提示元数据；后续由沙箱工具重新采集元数据。

生产 Windows 沙箱门禁保持关闭，无静默宿主执行回退。后续仍需 S2 环境准备、S3 工作区工具与元数据、S4 命令和 snapshot、S5 审批、S6 完整生命周期和复用、S7 CLI 接入，才能提供完整开箱即用沙箱。
