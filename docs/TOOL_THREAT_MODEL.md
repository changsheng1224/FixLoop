# 工具威胁模型与安全能力矩阵

> FixLoop 工具面威胁模型（Feature 5）。代码权威：`agent_runtime/path_safety.py`、`sensitive_paths.py`、`io_limits.py`、`worktree.py`、`security.py`、`tool_executor.py`（Gate 3/7）、`src/tools/sandbox_*`（Docker verify）。

---

## 1. 范围与假设

| 项 | 说明 |
|----|------|
| 信任边界 | 模型输出的工具参数**不可信**；宿主机 Agent 进程可信；Docker 沙箱半可信 |
| 资产 | 仓库源码、`.env`/密钥、宿主环境变量、网络、CPU/磁盘 |
| 明确不做 | 不把每次 `run_shell` 强制进 Docker（仍走宿主机 allowlist）；不替代 OS 级沙箱产品 |
| 启用开关 | `FIXLOOP_USE_WORKTREE=1` 启用每任务 Git Worktree；`FIXLOOP_*_MAX_BYTES` 覆盖体量上限 |

---

## 2. 威胁矩阵

| ID | 威胁 | 攻击面 | 既有/新增缓解 | Trace / 错误码 |
|----|------|--------|---------------|----------------|
| T1 | 路径穿越 / symlink 逃逸 | `read/write/patch/grep` 的 `path` | `path_safety.resolve_under_root`（Gate 3） | `path_escape` + `sandbox_violation` |
| T2 | 读写敏感密钥文件 | `.env`、`*.pem`、`id_rsa`、`credentials*`、`.ssh/`… | `sensitive_paths`（Gate 3 + 工具内防御） | `sensitive_path` |
| T3 | 超大文件撑爆上下文 | `read_file` | `io_limits.read_max_bytes`（默认 512KiB） | `oversized_read` |
| T4 | 二进制误当文本 | `read_file` | 魔数 / NUL / 不可打印比例检测 | `binary_file` |
| T5 | grep 结果过大 | `grep` | `max_results` + 字节截断 | 输出含 `[oversized_grep]` |
| T6 | 恶意 / 危险 shell | `run_shell` | `check_shell_command` allowlist（Gate 3）；Gate 7 默认 deny shell | `sandbox_violation` |
| T7 | shell 输出过大 / 密钥泄漏 | stdout/stderr | 字节截断 + `redact_text` + env 白名单 | （内容层） |
| T8 | 写高风险文件 | `write_file` / `patch_file` | 敏感路径拒绝；Gate 7 ask + diff 预览 | `sensitive_path` / `approval_denied` |
| T9 | 验证阶段逃逸宿主机 | pytest verify | Docker：`network_mode=none`、资源硬限、只读 rootfs | sandbox_* Trace |
| T10 | 任务取消后残留隔离目录 | worktree | 取消/结束时 `remove_worktree` | `worktree_created` / `worktree_removed` |
| T11 | 多任务互相污染工作区 | 并行 repair | 可选独立 Git Worktree（`FIXLOOP_USE_WORKTREE`） | 同上 |

---

## 3. 能力矩阵（工具 × 控制）

| 控制 | read | grep | write/patch | run_shell（宿主机） | sandbox_verify（Docker） |
|------|------|------|-------------|---------------------|--------------------------|
| 路径规范化 / 逃逸 | ✓ | ✓ | ✓ | cwd=root | 容器内 `/code` |
| 敏感路径拒绝 | ✓ | ✓ | ✓ | — | 镜像内策略 |
| 体量上限 | ✓ | ✓ | — | stdout/stderr | tar 上限 |
| 二进制拒绝 | ✓ | skip | — | — | — |
| 命令 allowlist | — | — | — | ✓ | 固定 pytest 入口 |
| 审批 Gate7 | auto | auto | ask | **deny** | 角色表 |
| 网络隔离 | — | — | — | 否（依赖 allowlist） | `network_mode=none` |
| 取消回收 | — | — | 快照回滚 | 进程树 kill | container.kill + worktree |

---

## 4. Trace 事件

| event | 何时 |
|-------|------|
| `tool_executed`（`tool_status=rejected`） | Gate 拒绝；`tool_error_code` 见上表 |
| `sandbox_violation` | worktree 创建失败等编排级违规（工具级多用 metadata） |
| `worktree_created` / `worktree_removed` | 启用 Worktree 时进入/离开 |

目录登记于 `canonical_trace.EVENT_CATALOG["security"]`。

---

## 5. 演示说明（安全执行）

```text
# 1) 路径逃逸 → Gate3 path_escape
read_file path=../outside.txt

# 2) 敏感文件 → sensitive_path
read_file path=.env

# 3) 超大文件 → oversized_read（或调低 FIXLOOP_READ_MAX_BYTES）
# 4) 恶意 shell → sandbox_violation（Gate3，早于 Gate7 deny）
run_shell command="sudo rm -rf /"

# 5) Worktree（可选）
set FIXLOOP_USE_WORKTREE=1
# repair 开始 → worktree_created；结束/取消 → worktree_removed
```

相关单测：`tests/test_tool_security_sandbox.py`、`tests/test_path_safety.py`、`tests/test_shell_security.py`。

---

## 6. 与既有文档关系

- Docker 四维隔离细节：`docs/bonus.md` §16、逃逸 Case
- 工具闸口：ToolExecutor Gate 1–9
- Canonical Trace：`docs/CANONICAL_TRACE.md`
