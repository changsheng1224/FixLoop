# S1：身份与资源归属

适用范围是本地单用户、多项目、并发修复。S1 统一资源的命名与归属，接入已有 sandbox 和 Workbench 生命周期；不提供多租户鉴权，也不把目录分区当成安全沙箱。

## 三种身份

| 身份 | 含义与生成方式 | 变化规则 |
| --- | --- | --- |
| `project_id` | `project-` + 原项目规范绝对路径的 SHA-256 前 16 位；Windows 使用 `normcase` | 同一注册路径稳定；独立 clone、移动到新路径默认是新项目 |
| `workspace_id` | 实际执行目录规范绝对路径的 SHA-256 前 16 位 | 每个 worktree / Workbench 执行副本各自独立 |
| `run_id` | 一次修复的执行 ID，由调用方分配并校验 | 同一项目内不复用；当前一次 repair 就是一个 task，因此 `task_id == run_id` |

项目身份不取 Git remote、branch 或 commit。相同 remote / commit 的两个 clone 默认互不共享项目资源。托管 worktree 通过创建时的 `repo_root` 保留原项目；Workbench 由控制器把注册的 `project_path` 显式传入 worker、`ProjectExecutor` 和 `SandboxTaskSpec.project_root`，不会从临时执行副本反推项目。

身份、目录实例、源码版本是三个概念。`workspace_id` / `project_id` 负责命名；sandbox binding 另外记录原项目及执行目录的 `(st_dev, st_ino)`，恢复和每次校验会拒绝目录替换。源码修改不会改变身份；已有 `content_version` 和 Workbench 内容快照用于版本校验，不能只用 commit SHA 描述脏工作区。

## 状态目录契约

`managed_storage_root()` 返回共享存储根；`managed_state_base()` 在其下返回项目分区。`SandboxTaskSpec.state_base` 和 worktree API 的 `state_base` 参数始终表示共享存储根，不能把 `managed_state_base()` 或 `binding.state_root` 的返回值再次当作共享根。工厂与 worktree handle 保存共享根，避免重复分区。

默认共享根为 Windows 的 `%LOCALAPPDATA%/FixLoop/state`，或 Linux 的 `$XDG_STATE_HOME/FixLoop/state`（默认 `~/.local/state/FixLoop/state`）。显式参数和 `FIXLOOP_STATE_ROOT` 也执行同样的项目分区。

```text
<shared storage>/
  projects/<project_id>/
    worktrees/<run_id>/
    worktree-leases/<run_id>.json
    tasks/<run_id>/
      sandbox-task.json
      ... 本次运行的状态、权限记录和回执
```

Workbench 仍有自己的控制器目录：`<state_dir>/runtime/<run_id>` 保存 worker 请求及运行状态，`<state_dir>/workspaces/<run_id>/tree` 保存执行副本。repair 把该 worker 状态目录作为 sandbox 共享根，因此 binding 位于 `runtime/<run_id>/projects/<project_id>/tasks/<run_id>/sandbox-task.json`。恢复控制器使用同一目录契约，并校验项目、工作区、run 和原项目目录实例；缺失归属信息或不可访问的记录不会被当成清理成功。

`trusted_state_root()` / `state_root_for()` 接收已经确定的最终状态目录，不再增加项目分区。这使已绑定 Agent 的 session、memory、observation 等状态继续落在同一 run 根下。所有 sandbox 状态必须在执行工作区、repository 和原项目之外。

## 资源归属

| 范围 | 资源 | S1 中的行为与约束 |
| --- | --- | --- |
| 产品 | 已验证的公共 runtime、模型与 tokenizer 文件 | 可共享；不据此共享任务数据或凭据，沿用现有环境管理 |
| 项目 | 项目配置、经确认的长期知识、与源码版本关联的派生缓存 | 定义归属键为 `project_id`；S1 不迁移或提升现有 run 内的记忆与缓存 |
| 会话 / 分支 | 对话历史、当前目标、祖先上下文与快照关联 | 沿用 Workbench SQLite 会话树，不因项目 ID 相同而合并历史 |
| 工作区 | 实际执行文件、目录实例、Git worktree 分支与所有权租约 | 托管 worktree 与执行副本独立；保留目录实例校验，禁止无所有权删除 |
| Run | working memory、权限票据、安装后的依赖环境、回执、候选补丁、日志与恢复记录 | 每个 run 使用独立外部状态；binding identity 含项目 / 工作区 / run，并传入已有权限与生命周期机制 |

Workbench 新项目行使用规范项目 ID；已发布数据库中的旧随机主键继续作为历史关系键，`project_record().project_id` 从注册路径给出统一运行时身份，不改写历史外键，也不新增数据库迁移。

## 恢复与边界

binding schema 为 2，持久化 `project_root`、`project_id`、原项目目录实例和实际 workspace mapping；读取时同时检查 checksum、策略 / 环境、路径、run 和目录实例。旧版未分区的 sandbox binding 与 worktree 租约不做自动搬迁或接管；Workbench 中缺少身份的未确认旧执行记录保持清理门禁关闭，避免把找错目录误判成无资源。已完成的会话历史仍可读取。

路径哈希是本地命名规则，不是访问权限或秘密。用户身份和跨用户隔离、缓存晋升与失效策略、记忆访问策略由后续阶段处理。本阶段也不改变已有 sandbox 的文件、进程、网络和权限边界。

## 验证

`tests/test_project_resource_identity.py` 覆盖共享显式 / 环境根下两个 clone 并发使用同名 run、worktree 的项目归属与运行分离、执行副本来源传递、原项目目录替换、跨项目恢复拒绝、Workbench 多会话与重开、恢复时查找真实 binding 及拒绝错误归属。相关回归覆盖已有 sandbox binding、worktree 清理、外部状态、repair factory 和 Workbench 行为；不要求外部模型或真实 WSL 演示。
