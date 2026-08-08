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
import os
from dataclasses import dataclass, field
from pathlib import Path

from agent_runtime.schema_utils import auto_validate
from agent_runtime.tool_rejection import (
    build_executor_cancel_metadata,
    build_executor_error_metadata,
    build_executor_rejection_metadata,
    build_gate7_pass_metadata,
)


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
    #   rejection_layer: "executor"（Gateway 拒绝在 middleware）
    #   gate_id: 1–9
    #   affected_paths: list[str]
    #   diff_summary: str


def _canonical_args_hash(tool_name: str, args: dict) -> str:
    """计算参数的稳定 hash（排序 key，忽略 runtime 差异）。"""
    import hashlib
    import json

    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    raw = f"{tool_name}:{canonical}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


class ToolExecutor:
    """工具执行闸口。

    Attributes:
        agent: Agent 实例（获取 tools registry, session, config, workspace）。
        approval_policy: "auto" | "ask" | "never"
    """

    def __init__(
        self,
        agent,
        approval_policy: str = "ask",
        dry_run: bool = False,
        quota: "QuotaEnforcer | None" = None,
    ):
        self.agent = agent
        self.approval_policy = approval_policy or agent.config.approval
        self.dry_run = dry_run
        self._quota = quota
        self._high_risk_tools = self._collect_high_risk()
        # 死循环检测滑动窗口
        self._call_window: list[str] = []

    def execute_gated(self, name: str, args: dict) -> ToolExecutionResult:
        """按序执行 Executor 闸口（Gate 1–9），不含 Gateway 权限层。"""
        from agent_runtime.repair_runtime import CanonicalToolCall

        pending = self.agent.session.pop("_pending_canonical_tool_call", {})
        call = CanonicalToolCall.create(
            name,
            args,
            source=pending.get("source", "native"),
            call_id=pending.get("call_id", ""),
        )
        self.agent.session["_last_canonical_tool_call"] = {
            "call_id": call.call_id,
            "name": call.name,
            "arguments": call.arguments,
            "source": call.source.value,
        }
        token = getattr(self.agent, "cancel_token", None)
        if token is not None and token.is_cancelled:
            return self._rejected_cancel("Error: 任务已取消，跳过工具执行。")

        allowed = self.agent._tool_names
        if name not in allowed:
            return self._rejected(
                1,
                "allowed_tools",
                f"Error: 工具 '{name}' 不在允许列表中。可用工具: {', '.join(sorted(allowed))}",
            )

        # ---- Gate 2: 工具存在检查 ----
        tool_spec = self.agent.tools.get(name)
        if tool_spec is None:
            return self._rejected(2, "not_found", f"Error: 工具 '{name}' 未注册。")

        # ---- Gate 3: 参数校验 ----
        validated_args, args_reject = self._validate_args(name, args)
        if args_reject is not None:
            return args_reject
        args = validated_args

        # ---- Gate 4: 配额检查 ----
        shell_slot_acquired, quota_reject = self._check_quota(name, tool_spec)
        if quota_reject is not None:
            return quota_reject

        try:
            return self._execute_after_quota(name, args, tool_spec, token)
        finally:
            if shell_slot_acquired and self._quota is not None:
                self._quota.release_shell()

    def _execute_after_quota(
        self,
        name: str,
        args: dict,
        tool_spec: dict,
        token,
    ) -> ToolExecutionResult:
        """执行 Gate 5–9；调用方负责释放 Gate 4 获取的资源。"""

        # ---- Gate 5: 重复调用检测 ----
        if self._is_duplicate(name, args):
            return self._rejected(
                5,
                "duplicate",
                f"Error: 重复调用检测：'{name}' 与最近调用完全相同，可能是死循环。"
                f"请尝试不同的参数或切换到其他工具。",
            )

        # ---- Gate 5.5: 死循环检测（滑动窗口内相同 tool+args_hash ≥ K） ----
        threshold = int(getattr(self.agent.config, "loop_detect_threshold", 0) or 0)
        if threshold > 0:
            call_hash = _canonical_args_hash(name, args)
            self._call_window.append(call_hash)
            if len(self._call_window) > threshold:
                self._call_window = self._call_window[-threshold:]
            if self._call_window.count(call_hash) >= threshold:
                return self._rejected(
                    5,
                    "loop_detected",
                    f"Error: 死循环检测——'{name}' 已连续或高频调用 {threshold} 次。"
                    f"请尝试不同的工具或策略。",
                )

        # ---- Gate 6: Dry-Run 模式（在审批之前，因为不实际修改） ----
        dry_run_result = self._maybe_dry_run(name, args)
        if dry_run_result is not None:
            return dry_run_result

        # ---- Gate 6.5: 高风险工具预览（审批时展示） ----
        patch_preview_meta, preview_reject = self._build_risk_preview(name, args)
        if preview_reject is not None:
            return preview_reject

        # ---- Gate 7: 分级审批检查 ----
        gate7_meta, approval_reject = self._check_approval(name, args, patch_preview_meta, token)
        if approval_reject is not None:
            return approval_reject

        # ---- Gate 8: 执行前工作区快照 ----
        is_risky = name in self._high_risk_tools
        before_snapshot = self._capture_snapshot() if is_risky else {}
        restore_snapshot = self._capture_restore_snapshot() if is_risky else {}

        # ---- Gate 9: 执行工具 ----
        execution_result = self._run_tool(name, args, tool_spec, token)
        if isinstance(execution_result, ToolExecutionResult):
            execution_result.metadata.setdefault(
                "execution_tier", tool_spec.get("execution_tier", "host")
            )
            if gate7_meta:
                execution_result.metadata.update(gate7_meta)
            if patch_preview_meta:
                execution_result.metadata["patch_preview"] = patch_preview_meta
            if self._quota is not None:
                self._quota.record(name, tool_spec)
            return execution_result
        result_text = execution_result

        # ---- Gate 9 续: 执行后快照对比 ----
        metadata = self._build_success_metadata(
            name,
            tool_spec,
            gate7_meta,
            patch_preview_meta,
        )
        if is_risky:
            if token is not None and token.is_cancelled:
                self._restore_restore_snapshot(restore_snapshot)
                metadata["cancel_restored"] = True
                after_snapshot = self._capture_snapshot()
            else:
                after_snapshot = self._capture_snapshot()
            metadata.update(self._diff_snapshots(before_snapshot, after_snapshot))

        # 记录配额
        if self._quota is not None:
            self._quota.record(name, tool_spec)

        return ToolExecutionResult(content=result_text, metadata=metadata)

    def execute(self, name: str, args: dict) -> ToolExecutionResult:
        """兼容旧调用；生产路径应经 Agent.execute_tool → dispatch。"""
        return self.execute_gated(name, args)

    # ---- 内部方法 ----

    def _rejected(
        self, gate_id: int, tool_error_code: str, content: str, **extra
    ) -> ToolExecutionResult:
        """构造 Executor 闸口拒绝结果。"""
        try:
            from agent_runtime.metrics import get_registry

            get_registry().counter_inc(
                "fixloop_security_denials_total",
                labels={"reason": str(tool_error_code)},
            )
        except Exception:
            pass
        return ToolExecutionResult(
            content=content,
            metadata=build_executor_rejection_metadata(gate_id, tool_error_code, **extra),
        )

    def _rejected_cancel(self, content: str, **extra) -> ToolExecutionResult:
        """用户 cancel 导致的拒绝（rejection_layer=cancel）。"""
        return ToolExecutionResult(
            content=content,
            metadata=build_executor_cancel_metadata(**extra),
        )

    def _validate_args(self, name: str, args: dict) -> tuple[dict, ToolExecutionResult | None]:
        """Gate 3：dataclass 参数校验与路径逃逸校验。"""
        from agent_runtime.tool_schema import validate_tool_arguments

        raw_schema = ((self.agent.tools or {}).get(name) or {}).get("schema", {})
        normalized, shape_errors = validate_tool_arguments(raw_schema, args)
        if shape_errors:
            return args, self._rejected(
                3,
                "invalid_args",
                f"Error: 参数校验失败: {shape_errors}",
                error_code="invalid_arguments",
                retryable=True,
                expected=((self.agent.tools or {}).get(name) or {}).get("schema", {}),
                provided=sorted(args) if isinstance(args, dict) else [],
            )
        args = normalized
        args_dataclass = self._get_args_class(name)
        if args_dataclass:
            try:
                args = auto_validate(args_dataclass, args)
            except ValueError as e:
                return args, self._rejected(3, "invalid_args", f"Error: 参数校验失败: {e}")

        path_reject = self._validate_path_args(name, args)
        if path_reject is not None:
            return args, path_reject
        shell_reject = self._validate_shell_args(name, args)
        if shell_reject is not None:
            return args, shell_reject
        return args, None

    def _check_quota(
        self, name: str, tool_spec: dict | None = None
    ) -> tuple[bool, ToolExecutionResult | None]:
        """Gate 4：检查调用配额，并返回是否获取了 shell 并发槽。"""
        if self._quota is None:
            return False, None
        decision = self._quota.decision(name, tool_spec)
        if not self._quota.check(name, tool_spec):
            extra = {}
            if decision is not None:
                self._quota.record_rejection(name, tool_spec)
                extra = {
                    "budget_group": decision.group.value,
                    "budget_used": decision.used,
                    "budget_limit": decision.limit,
                    "budget_remaining": max(0, decision.limit - decision.used),
                    "required_next_action": "choose_tool_from_available_budget_group",
                }
            return False, self._rejected(
                4,
                "quota_exceeded",
                f"Error: 工具 '{name}' 超出配额限制。{self._quota.status()}",
                **extra,
            )
        if name == "run_shell":
            if not self._quota.acquire_shell():
                return False, self._rejected(
                    4,
                    "quota_exceeded",
                    f"Error: 并行 shell 达到上限。{self._quota.status()}",
                )
            return True, None
        return False, None

    def _maybe_dry_run(self, name: str, args: dict) -> ToolExecutionResult | None:
        """Gate 6：dry-run 直接返回计划，不进入审批与执行。"""
        if not self.dry_run:
            return None
        return ToolExecutionResult(
            content=f"[DRY RUN] Would {name}({args})",
            metadata={"tool_status": "success", "dry_run": True},
        )

    def _build_risk_preview(
        self,
        name: str,
        args: dict,
    ) -> tuple[dict | None, ToolExecutionResult | None]:
        """Gate 6.5：为写类工具准备审批预览。"""
        if name == "write_file":
            raw_path = args.get("path", "")
            content_len = len(str(args.get("content", "")))
            mode = "追加" if args.get("append") else "新建/覆盖"
            preview_text = f"[{mode}] {raw_path} ({content_len} 字符)"
            return {"path": raw_path, "preview_text": preview_text}, None
        if name == "patch_file":
            patch_preview_meta, preview_err = self._build_patch_preview(args)
            if preview_err:
                return None, self._rejected(
                    3,
                    "invalid_args",
                    f"Error: 补丁预览失败: {preview_err}",
                )
            return patch_preview_meta, None
        return None, None

    def _check_approval(
        self,
        name: str,
        args: dict,
        patch_preview_meta: dict | None,
        token,
    ) -> tuple[dict | None, ToolExecutionResult | None]:
        """Gate 7：执行分级审批策略。"""
        tier = self._approval_tier(name)
        if tier == self._APPROVAL_TIER_DENY:
            return None, self._rejected(
                7,
                "approval_denied",
                f"Error: 工具 '{name}' 已被禁止执行。",
            )
        if tier != self._APPROVAL_TIER_ASK:
            return None, None

        if self._approve(name, args, patch_preview_meta):
            return build_gate7_pass_metadata(self.approval_policy), None

        if token is not None and token.is_cancelled:
            return None, self._rejected_cancel("Error: 任务已取消（审批中断）。")

        extra = {"approval_policy": self.approval_policy}
        if patch_preview_meta:
            extra["patch_preview"] = patch_preview_meta
        return None, self._rejected(
            7,
            "approval_denied",
            f"Error: 工具 '{name}' 调用被拒绝（approval_policy={self.approval_policy}）",
            **extra,
        )

    def _run_tool(self, name: str, args: dict, tool_spec: dict, token) -> str | ToolExecutionResult:
        """Gate 9：运行工具并把异常转换为结构化结果。"""
        if hasattr(self.agent.config, "effective_deadline"):
            timeout_s = int(self.agent.config.effective_deadline()["tool_s"] or 0)
        else:
            timeout_s = int(getattr(self.agent.config, "tool_timeout_s", 0) or 0)
        deadline = getattr(self.agent, "_repair_deadline", None)
        remaining = deadline.remaining_s() if deadline is not None else None
        if remaining is not None:
            timeout_s = max(1, int(remaining)) if timeout_s <= 0 else max(
                1, min(timeout_s, int(remaining))
            )
        ctx = self.agent.tool_context
        prev_ctx_token = ctx.cancel_token
        # write/patch 必须等工具返回后再 restore；只读/shell 可协作式中断
        run_cancel = token if name not in ("write_file", "patch_file", "apply_patch") else None
        ctx.cancel_token = token
        try:
            from agent_runtime.cancellation import CancelledError
            from agent_runtime.tool_timeout import ToolTimeoutError, run_with_timeout

            result = run_with_timeout(
                lambda: tool_spec["run"](args),
                timeout_s=timeout_s,
                cancel_token=run_cancel,
            )
            if hasattr(result, "metadata") and hasattr(result, "content"):
                metadata = dict(result.metadata or {})
                if getattr(result, "structured_facts", None):
                    metadata["structured_facts"] = list(result.structured_facts)
                if getattr(result, "raw", None):
                    metadata["raw_result"] = result.raw
                return ToolExecutionResult(content=str(result.content), metadata=metadata)
            return result
        except CancelledError:
            return ToolExecutionResult(
                content=f"Error: 工具 '{name}' 执行已取消。",
                metadata=build_executor_cancel_metadata(),
            )
        except ToolTimeoutError as e:
            return ToolExecutionResult(
                content=f"Error: 工具 '{name}' 执行超时（{e.timeout_s} 秒）",
                metadata=build_executor_error_metadata("tool_timeout", timeout_s=e.timeout_s),
            )
        except Exception as e:
            return ToolExecutionResult(
                content=f"Error: 工具 '{name}' 执行异常: {e}",
                metadata=build_executor_error_metadata(),
            )
        finally:
            ctx.cancel_token = prev_ctx_token

    def _build_success_metadata(
        self,
        name: str,
        tool_spec: dict,
        gate7_meta: dict | None,
        patch_preview_meta: dict | None,
    ) -> dict:
        """Gate 9 后处理：构造成功结果 metadata。"""
        metadata = {"tool_status": "success"}
        execution_tier = tool_spec.get("execution_tier", "host")
        if execution_tier:
            metadata["execution_tier"] = execution_tier
        if gate7_meta:
            metadata.update(gate7_meta)
        if patch_preview_meta:
            metadata["patch_preview"] = patch_preview_meta
        if name == "run_shell":
            provider = getattr(self.agent.tool_context, "shell_env_provider", None)
            if callable(provider):
                metadata["shell_env_keys"] = sorted(provider().keys())
        return metadata

    def _validate_path_args(self, name: str, args) -> ToolExecutionResult | None:
        """Gate 3 续：路径 resolve、敏感路径、超大/二进制读拦截。"""
        if isinstance(args, dict):
            raw_path = args.get("path")
        else:
            raw_path = getattr(args, "path", None)
        if not raw_path:
            return None
        try:
            resolved = self.agent.tool_context.resolve(str(raw_path))
        except ValueError as e:
            msg = str(e)
            code = "path_escape" if "逃逸" in msg else "invalid_args"
            return self._rejected(
                3,
                code,
                f"Error: 路径校验失败: {e}",
                sandbox_violation=code == "path_escape",
            )

        from agent_runtime.sensitive_paths import (
            check_sensitive_access,
            sensitive_reject_message,
        )

        sens = check_sensitive_access(name, raw_path) or check_sensitive_access(
            name, resolved
        )
        if sens:
            return self._rejected(
                3,
                sens,
                sensitive_reject_message(raw_path),
                sandbox_violation=True,
            )

        # read_file：体量与二进制护栏（文件存在时）
        if name == "read_file" and resolved.is_file():
            from agent_runtime.io_limits import is_likely_binary, read_max_bytes

            try:
                size = resolved.stat().st_size
            except OSError:
                size = 0
            limit = read_max_bytes()
            if size > limit:
                return self._rejected(
                    3,
                    "oversized_read",
                    f"Error: 文件过大 ({size} bytes > {limit})，拒绝读取: {raw_path}",
                )
            if is_likely_binary(resolved):
                return self._rejected(
                    3,
                    "binary_file",
                    f"Error: 疑似二进制文件，拒绝读取: {raw_path}",
                )
        return None

    def _validate_shell_args(self, name: str, args) -> ToolExecutionResult | None:
        """Gate 3 续：宿主机 shell 命令 allowlist（Docker verify 不经此路径）。"""
        if name != "run_shell":
            return None
        if isinstance(args, dict):
            command = args.get("command", "")
        else:
            command = getattr(args, "command", "") or ""
        if not command:
            return None
        from agent_runtime.security import check_shell_command

        allowed, reason = check_shell_command(str(command))
        if allowed:
            return None
        return self._rejected(
            3,
            "sandbox_violation",
            f"Error: Shell 命令被安全策略拒绝 ({reason}): {str(command)[:100]}",
            sandbox_violation=True,
        )

    # Gate 7 分级审批：auto(读类)/ask(写类)/deny(禁止)
    _APPROVAL_TIER_AUTO = "auto"
    _APPROVAL_TIER_ASK = "ask"
    _APPROVAL_TIER_DENY = "deny"

    _READ_TOOLS_FOR_APPROVAL = frozenset(
        {
            "read_file",
            "list_files",
            "search",
            "grep",
            "ast_parse",
            "inspect_file",
            "find_test",
            "git_blame",
            "git_diff",
            "github_list_issues",
            "github_get_issue",
            "github_list_issue_comments",
            "github_get_repo",
            "github_list_commits",
            "github_get_commit",
            "github_list_branches",
            "github_list_pull_requests",
            "github_get_pull_request",
            "github_list_workflow_runs",
        }
    )
    _ASK_TOOLS = frozenset({"write_file", "patch_file", "github_create_draft_pr"})
    _DENY_TOOLS = frozenset({"run_shell"})

    @classmethod
    def _approval_tier(cls, name: str) -> str:
        """返回工具的分级审批等级。"""
        if name in cls._READ_TOOLS_FOR_APPROVAL:
            return cls._APPROVAL_TIER_AUTO
        if name in cls._ASK_TOOLS:
            return cls._APPROVAL_TIER_ASK
        if name in cls._DENY_TOOLS:
            return cls._APPROVAL_TIER_DENY
        return cls._APPROVAL_TIER_ASK  # 未知工具默认须审批

    def _collect_high_risk(self) -> set[str]:
        """collect_high_risk 已废弃，保留兼容 stub。"""
        return self._ASK_TOOLS | self._DENY_TOOLS

    def _get_args_class(self, name: str) -> type | None:
        """根据工具名返回对应的参数 dataclass。"""
        tool = (self.agent.tools or {}).get(name) or {}
        if tool.get("args_dataclass") is not None:
            return tool["args_dataclass"]
        from agent_runtime import tools as tools_module

        mapping = {
            "list_files": tools_module.ListFilesArgs,
            "read_file": tools_module.ReadFileArgs,
            "search": tools_module.SearchArgs,
            "grep": tools_module.GrepArgs,
            "write_file": tools_module.WriteFileArgs,
            "patch_file": tools_module.PatchFileArgs,
            "run_shell": tools_module.RunShellArgs,
        }
        return mapping.get(name)

    # 读类工具：仅按 (name, path) 语义去重（不比较 start/end/pattern 等参数）
    _READ_TOOLS = frozenset(
        {
            "read_file",
            "list_files",
            "search",
            "grep",
            "ast_parse",
            "inspect_file",
            "find_test",
            "git_blame",
            "git_diff",
            "github_list_issues",
            "github_get_issue",
            "github_list_issue_comments",
            "github_get_repo",
            "github_list_commits",
            "github_get_commit",
            "github_list_branches",
            "github_list_pull_requests",
            "github_get_pull_request",
            "github_list_workflow_runs",
        }
    )

    def _is_duplicate(self, name: str, args: dict) -> bool:
        """检查最近 2 次调用是否重复。

        - 读类工具：相同 tool_name + 相同 path → 语义重复
        - 写类工具：name + args 完全匹配 → 精确重复
        """
        history = self.agent.session.get("history", [])
        tool_calls = [h for h in history if h.get("tool_name")]
        recent = tool_calls[-2:]
        if len(recent) < 2:
            return False

        if name in self._READ_TOOLS:
            same_name = recent[0].get("tool_name") == recent[1].get("tool_name") == name
            same_path = (
                recent[0].get("tool_args", {}).get("path", "")
                == recent[1].get("tool_args", {}).get("path", "")
                == args.get("path", "")
            )
            return same_name and same_path
        else:
            same_name = recent[0].get("tool_name") == recent[1].get("tool_name") == name
            same_args = recent[0].get("tool_args") == recent[1].get("tool_args") == args
            return same_name and same_args

    def _approve(self, name: str, args: dict, patch_preview: dict | None = None) -> bool:
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
            if patch_preview and name in ("patch_file", "write_file"):
                prompt = (
                    f"\n⚠ 审批: {name} → {patch_preview.get('path', args.get('path', ''))}\n"
                    f"{patch_preview.get('preview_text', '')}\n"
                    f"  输入 'y' 批准，其他键拒绝: "
                )
            else:
                prompt = (
                    f"\n⚠ 审批: 允许执行高风险工具 '{name}'?\n"
                    f"  参数: {args}\n"
                    f"  输入 'y' 批准，其他键拒绝: "
                )
            response = input(prompt)
            return response.strip().lower() == "y"
        except (OSError, EOFError):
            return False  # 非交互环境 → 拒绝
        except KeyboardInterrupt:
            token = getattr(self.agent, "cancel_token", None)
            if token is not None:
                token.cancel("user")
            return False
        except EOFError:
            return False

    def _build_patch_preview(self, args: dict) -> tuple[dict | None, str | None]:
        """为 patch_file 构建 apply 前预览；失败返回错误信息。"""
        from agent_runtime.patch_engine import try_build_patch_preview

        raw_path = args.get("path", "")
        if not raw_path:
            return None, "缺少必填参数 path"
        try:
            target = self.agent.tool_context.resolve(raw_path)
        except ValueError as e:
            return None, str(e)
        if not target.exists():
            return None, f"文件不存在: {raw_path}"
        if not target.is_file():
            return None, f"不是文件: {raw_path}"
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return None, f"无法以 UTF-8 编码读取: {raw_path}"
        except OSError as e:
            return None, f"读取文件失败: {e}"
        return try_build_patch_preview(raw_path, text, args)

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

    def _capture_restore_snapshot(self) -> dict[str, dict]:
        """Gate 8 内容快照：支持二进制、权限位与符号链接回滚。"""
        root = Path(self.agent.tool_context.root)
        if not root.exists():
            return {}
        snapshot: dict[str, dict] = {}
        for fpath in root.rglob("*"):
            if (fpath.is_file() or fpath.is_symlink()) and not self._is_ignored(fpath):
                try:
                    rel = str(fpath.relative_to(root))
                    stat = fpath.lstat()
                    if fpath.is_symlink():
                        snapshot[rel] = {
                            "kind": "symlink",
                            "target": os.readlink(fpath),
                            "mode": stat.st_mode,
                        }
                    else:
                        snapshot[rel] = {
                            "kind": "file",
                            "bytes": fpath.read_bytes(),
                            "mode": stat.st_mode,
                        }
                except (OSError, ValueError):
                    pass
        return snapshot

    def _restore_restore_snapshot(self, before: dict[str, dict]) -> None:
        """将 workspace 恢复到 Gate 8 内容快照。"""
        root = Path(self.agent.tool_context.root)
        if not root.exists():
            return
        before_paths = set(before.keys())
        current_paths: set[str] = set()
        for fpath in root.rglob("*"):
            if (fpath.is_file() or fpath.is_symlink()) and not self._is_ignored(fpath):
                try:
                    current_paths.add(str(fpath.relative_to(root)))
                except ValueError:
                    pass
        for rel, record in before.items():
            try:
                from agent_runtime.path_safety import resolve_under_root

                target = resolve_under_root(root, rel)
            except (ValueError, OSError):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                if target.exists() or target.is_symlink():
                    target.unlink()
                # Backwards compatibility for snapshots created by older
                # runtimes/tests that stored plain UTF-8 strings.
                if isinstance(record, str) or record is None:
                    if record is not None:
                        target.write_text(record, encoding="utf-8")
                    continue
                if record.get("kind") == "symlink":
                    os.symlink(record.get("target", ""), target)
                else:
                    target.write_bytes(record.get("bytes", b""))
                    if record.get("mode") is not None:
                        target.chmod(record["mode"])
            except OSError:
                continue
        for rel in sorted(current_paths - before_paths):
            target = root / rel
            if target.is_file():
                target.unlink()

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

    限制每类工具在单次会话中的最大调用次数 + 并发 subprocess 上限。
    """

    def __init__(
        self,
        max_writes: int = 20,
        max_shell: int = 10,
        max_total: int = 50,
        max_concurrent_shell: int = 3,
        *,
        group_limits: dict | None = None,
    ):
        import threading

        from agent_runtime.tool_budget import ToolBudgetLedger

        self._limits = {"write": max_writes, "shell": max_shell, "total": max_total}
        self._counts = {"write": 0, "shell": 0, "total": 0}
        self._shell_semaphore = threading.Semaphore(max_concurrent_shell)
        self._group_ledger = ToolBudgetLedger(group_limits) if group_limits is not None else None

    def _group_for(self, tool_name: str, tool_spec: dict | None = None):
        from agent_runtime.tool_budget import infer_tool_budget_group

        return infer_tool_budget_group(tool_name, tool_spec)

    def decision(self, tool_name: str, tool_spec: dict | None = None):
        if self._group_ledger is None:
            return None
        return self._group_ledger.check(self._group_for(tool_name, tool_spec))

    def record_rejection(self, tool_name: str, tool_spec: dict | None = None) -> None:
        if self._group_ledger is not None:
            self._group_ledger.record_rejection(self._group_for(tool_name, tool_spec))

    def acquire_shell(self) -> bool:
        return self._shell_semaphore.acquire(blocking=False)

    def release_shell(self):
        self._shell_semaphore.release()

    def check(self, tool_name: str, tool_spec: dict | None = None) -> bool:
        """检查工具是否在配额内。

        Args:
            tool_name: 工具名称。

        Returns:
            True 如果允许执行。
        """
        decision = self.decision(tool_name, tool_spec)
        if decision is not None:
            return decision.allowed
        if self._counts["total"] >= self._limits["total"]:
            return False
        if tool_name in ("write_file", "patch_file", "apply_patch"):
            return self._counts["write"] < self._limits["write"]
        if tool_name == "run_shell":
            return self._counts["shell"] < self._limits["shell"]
        return True  # 只读工具不受限

    def record(self, tool_name: str, tool_spec: dict | None = None):
        """记录一次工具调用。"""
        if self._group_ledger is not None:
            self._group_ledger.record(self._group_for(tool_name, tool_spec))
        self._counts["total"] += 1
        if tool_name in ("write_file", "patch_file", "apply_patch"):
            self._counts["write"] += 1
        elif tool_name == "run_shell":
            self._counts["shell"] += 1

    def status(self) -> str:
        """返回当前配额使用情况。"""
        cnt = self._counts
        lim = self._limits
        legacy = (
            f"配额: writes {cnt['write']}/{lim['write']}, "
            f"shell {cnt['shell']}/{lim['shell']}, "
            f"total {cnt['total']}/{lim['total']}"
        )
        if self._group_ledger is None:
            return legacy
        groups = self._group_ledger.summary()
        compact = ", ".join(
            f"{name} {values['used']}/{values['limit']}" for name, values in groups.items()
        )
        return f"{legacy}; groups: {compact}"

    def quota_summary(self) -> dict:
        """返回结构化配额使用数据（供 report.json）。"""
        summary = {
            "writes": {"used": self._counts["write"], "limit": self._limits["write"]},
            "shell": {"used": self._counts["shell"], "limit": self._limits["shell"]},
            "total": {"used": self._counts["total"], "limit": self._limits["total"]},
        }
        if self._group_ledger is not None:
            summary["groups"] = self._group_ledger.summary()
        return summary


def _sha256_file(path: Path) -> str:
    """计算文件的 SHA256 哈希。"""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
