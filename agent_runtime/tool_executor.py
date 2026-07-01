"""工具执行闸口：9 道安全检查，按序执行。

所有工具调用必须经过此闸口，每道闸口失败返回结构化结果，不抛异常。

检查顺序：
1. allowed_tools 白名单
2. 工具存在检查
3. 参数校验（含路径逃逸检测）
4. 配额检查（writes/shell/total）
5. 重复调用检测（最近 2 次完全相同 → 拒绝）
6. Dry-Run 模式（返回计划，跳过后续）
7. 审批检查（高风险工具根据 approval_policy）
8. 执行前工作区快照
9. 执行工具 + 执行后快照对比（生成 affected_paths）
"""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from agent_runtime.schema_utils import auto_validate


@dataclass
class ToolExecutionResult:
    """工具执行结果。

    无论成功或失败，都返回此结构。不抛异常。
    """

    content: str  # 工具输出或错误描述
    metadata: dict = field(default_factory=dict)
    # metadata 包含:
    #   tool_status: "success" | "rejected" | "error"
    #   tool_error_code: "allowed_tools" | "not_found" | "invalid_args" |
    #                    "duplicate" | "approval_denied" | "runtime_error"
    #   affected_paths: list[str]
    #   diff_summary: str


class ToolExecutor:
    """工具执行闸口。

    Attributes:
        agent: Agent 实例（获取 tools registry, session, config, workspace）。
        approval_policy: "auto" | "ask" | "never"
    """

    def __init__(
        self, agent, approval_policy: str = "ask",
        dry_run: bool = False, quota: "QuotaEnforcer | None" = None,
    ):
        self.agent = agent
        self.approval_policy = approval_policy or agent.config.approval
        self.dry_run = dry_run
        self._quota = quota
        self._high_risk_tools = self._collect_high_risk()

    def execute(self, name: str, args: dict) -> ToolExecutionResult:
        """按序执行 7 道闸口，返回结构化结果。

        Args:
            name: 工具名称。
            args: 工具参数字典。

        Returns:
            ToolExecutionResult 实例。
        """
        # ---- Gate 1: allowed_tools 白名单 ----
        allowed = self.agent._tool_names
        if name not in allowed:
            return ToolExecutionResult(
                content=f"Error: 工具 '{name}' 不在允许列表中。"
                f"可用工具: {', '.join(sorted(allowed))}",
                metadata={"tool_status": "rejected", "tool_error_code": "allowed_tools"},
            )

        # ---- Gate 2: 工具存在检查 ----
        tool_spec = self.agent.tools.get(name)
        if tool_spec is None:
            return ToolExecutionResult(
                content=f"Error: 工具 '{name}' 未注册。",
                metadata={"tool_status": "rejected", "tool_error_code": "not_found"},
            )

        # ---- Gate 3: 参数校验 ----
        args_dataclass = self._get_args_class(name)
        if args_dataclass:
            try:
                args = auto_validate(args_dataclass, args)
            except ValueError as e:
                return ToolExecutionResult(
                    content=f"Error: 参数校验失败: {e}",
                    metadata={"tool_status": "rejected", "tool_error_code": "invalid_args"},
                )

        # ---- Gate 4: 配额检查 ----
        if self._quota is not None:
            if not self._quota.check(name):
                return ToolExecutionResult(
                    content=f"Error: 工具 '{name}' 超出配额限制。{self._quota.status()}",
                    metadata={"tool_status": "rejected", "tool_error_code": "quota_exceeded"},
                )

        # ---- Gate 5: 重复调用检测 ----
        if self._is_duplicate(name, args):
            return ToolExecutionResult(
                content=f"Error: 重复调用检测：'{name}' 与最近调用完全相同，可能是死循环。"
                f"请尝试不同的参数或切换到其他工具。",
                metadata={"tool_status": "rejected", "tool_error_code": "duplicate"},
            )

        # ---- Gate 6: Dry-Run 模式（在审批之前，因为不实际修改） ----
        if self.dry_run:
            return ToolExecutionResult(
                content=f"[DRY RUN] Would {name}({args})",
                metadata={"tool_status": "success", "dry_run": True},
            )

        # ---- Gate 7: 审批检查 ----
        if name in self._high_risk_tools:
            if not self._approve(name, args):
                return ToolExecutionResult(
                    content=(
                        f"Error: 工具 '{name}' 调用被拒绝"
                        f"（approval_policy={self.approval_policy}）"
                    ),
                    metadata={"tool_status": "rejected", "tool_error_code": "approval_denied"},
                )

        # ---- Gate 8: 执行前工作区快照 ----
        is_risky = name in self._high_risk_tools
        before_snapshot = self._capture_snapshot() if is_risky else {}

        # ---- Gate 9: 执行工具 ----
        try:
            result_text = tool_spec["run"](args)
        except Exception as e:
            return ToolExecutionResult(
                content=f"Error: 工具 '{name}' 执行异常: {e}",
                metadata={"tool_status": "error", "tool_error_code": "runtime_error"},
            )

        # ---- Gate 9 续: 执行后快照对比 ----
        metadata = {"tool_status": "success"}
        if is_risky:
            after_snapshot = self._capture_snapshot()
            metadata.update(self._diff_snapshots(before_snapshot, after_snapshot))

        # 记录配额
        if self._quota is not None:
            self._quota.record(name)

        return ToolExecutionResult(content=result_text, metadata=metadata)

    # ---- 内部方法 ----

    def _collect_high_risk(self) -> set[str]:
        """收集所有标记为 risky=True 的工具名。"""
        return {name for name, spec in self.agent.tools.items() if spec.get("risky")}

    def _get_args_class(self, name: str) -> type | None:
        """根据工具名返回对应的参数 dataclass。"""
        from agent_runtime import tools as tools_module

        mapping = {
            "list_files": tools_module.ListFilesArgs,
            "read_file": tools_module.ReadFileArgs,
            "search": tools_module.SearchArgs,
            "write_file": tools_module.WriteFileArgs,
            "patch_file": tools_module.PatchFileArgs,
            "run_shell": tools_module.RunShellArgs,
        }
        return mapping.get(name)

    def _is_duplicate(self, name: str, args: dict) -> bool:
        """检查最近 2 次工具调用的 name+args 是否与本次完全相同。"""
        history = self.agent.session.get("history", [])
        # 提取有 tool_name 的记录
        tool_calls = [
            h for h in history
            if h.get("tool_name")
        ]
        recent = tool_calls[-2:]
        if len(recent) < 2:
            return False

        same_name = recent[0].get("tool_name") == recent[1].get("tool_name") == name
        same_args = recent[0].get("tool_args") == recent[1].get("tool_args") == args
        return same_name and same_args

    def _approve(self, name: str, args: dict) -> bool:
        """审批检查。
        - "auto" → True
        - "never" → False
        - "ask" → 交互式询问（CLI 环境）
        """
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            return False
        # "ask" 模式
        try:
            response = input(
                f"\n⚠ 审批: 允许执行高风险工具 '{name}'?\n"
                f"  参数: {args}\n"
                f"  输入 'y' 批准，其他键拒绝: "
            )
            return response.strip().lower() == "y"
        except (EOFError, KeyboardInterrupt):
            return False

    def _capture_snapshot(self) -> dict[str, str]:
        """对 workspace 根目录下所有文件做 SHA256 快照。"""
        root = Path(self.agent.tool_context.root)
        if not root.exists():
            return {}
        snapshot = {}
        for fpath in root.rglob("*"):
            if fpath.is_file() and not self._is_ignored(fpath):
                try:
                    rel = fpath.relative_to(root)
                    snapshot[str(rel)] = _sha256_file(fpath)
                except (OSError, ValueError):
                    pass
        return snapshot

    def _is_ignored(self, path: Path) -> bool:
        """检查路径是否在忽略目录中。"""
        from agent_runtime.tools import IGNORED_PATH_NAMES

        parts = set(path.parts)
        return bool(parts & IGNORED_PATH_NAMES)

    def _diff_snapshots(self, before: dict, after: dict) -> dict:
        """对比前后快照，生成 affected_paths 和 diff_summary。"""
        affected = set()
        all_paths = set(before.keys()) | set(after.keys())
        for p in all_paths:
            if before.get(p) != after.get(p):
                affected.add(p)

        summary_parts = []
        for p in sorted(affected):
            if p in before and p in after:
                summary_parts.append(f"M  {p}")
            elif p in after:
                summary_parts.append(f"+  {p}")
            else:
                summary_parts.append(f"-  {p}")

        return {
            "affected_paths": sorted(affected),
            "diff_summary": "\n".join(summary_parts) if summary_parts else "(无变更)",
        }


class QuotaEnforcer:
    """工具执行配额控制。

    限制每类工具在单次会话中的最大调用次数。
    """

    def __init__(self, max_writes: int = 20, max_shell: int = 10, max_total: int = 50):
        self._limits = {"write": max_writes, "shell": max_shell, "total": max_total}
        self._counts = {"write": 0, "shell": 0, "total": 0}

    def check(self, tool_name: str) -> bool:
        """检查工具是否在配额内。

        Args:
            tool_name: 工具名称。

        Returns:
            True 如果允许执行。
        """
        if self._counts["total"] >= self._limits["total"]:
            return False
        if tool_name in ("write_file", "patch_file"):
            return self._counts["write"] < self._limits["write"]
        if tool_name == "run_shell":
            return self._counts["shell"] < self._limits["shell"]
        return True  # 只读工具不受限

    def record(self, tool_name: str):
        """记录一次工具调用。"""
        self._counts["total"] += 1
        if tool_name in ("write_file", "patch_file"):
            self._counts["write"] += 1
        elif tool_name == "run_shell":
            self._counts["shell"] += 1

    def status(self) -> str:
        """返回当前配额使用情况。"""
        cnt = self._counts
        lim = self._limits
        return (
            f"配额: writes {cnt['write']}/{lim['write']}, "
            f"shell {cnt['shell']}/{lim['shell']}, "
            f"total {cnt['total']}/{lim['total']}"
        )


def _sha256_file(path: Path) -> str:
    """计算文件的 SHA256 哈希。"""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
