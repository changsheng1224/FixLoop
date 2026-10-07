"""Repair convergence, grounding, action projection and recovery decisions (L2)."""

from __future__ import annotations

from agent_runtime.loop_policy import LoopPolicy
from agent_runtime.tool_result import ToolResult


def tool_target_paths(tool_name: str, tool_args: dict | None) -> list[str]:
    """Return normalized file targets for write/recovery decisions.

    ``apply_patch`` carries its paths in the patch envelope rather than a
    top-level ``path`` argument.  Recovery must use those exact paths so a
    stale write cannot fall back to a wildcard read reservation.
    """
    args = tool_args or {}
    direct = str(args.get("path") or "").replace("\\", "/").strip()
    if tool_name != "apply_patch":
        return [direct] if direct else []

    patch_text = args.get("patch") or args.get("diff") or args.get("input") or ""
    if not str(patch_text).strip():
        return []
    try:
        from agent_runtime.apply_patch_format import parse_apply_patch_text

        ops = parse_apply_patch_text(str(patch_text))
    except (TypeError, ValueError):
        return []
    paths: list[str] = []
    for op in ops:
        path = str(getattr(op, "path", "") or "").replace("\\", "/").strip()
        if path and path not in paths:
            paths.append(path)
    return paths


class RepairLoopPolicy(LoopPolicy):
    localization_mode = True
    action_directive = (
        "[PATCH DECISION REQUIRED] Exploration is closed. Call apply_patch/patch_file now, "
        "or call finish_repair with a concise cannot_patch/needs_more_context reason "
        "grounded in the evidence ledger."
    )
    stall_hint = (
        " 【patcher】下一步必须对实现文件 apply_patch（含 - 上下文）；"
        "不要继续纯 read/grep；不要用 run_shell/sandbox_test。"
    )

    def reset(self, context):
        context.agent.session.pop("_patcher_runtime", None)

    def output_recovery(self, *, truncated):
        prefix = "[OUTPUT RECOVERY]" if truncated else "[EMPTY OUTPUT RECOVERY]"
        return (
            f"{prefix} The previous output was discarded. Do not repeat the analysis. "
            "Produce exactly one apply_patch/patch_file call, or call finish_repair with "
            "a grounded cannot_patch/needs_more_context reason. No broad exploration."
        )

    def on_result(self, context, tool_name, tool_args, result, *, step):
        self.recover_result(context, tool_name, tool_args, result, step=step)
        if result.status == "success":
            self.sync_grounding(context, tool_name, tool_args, result)

    def feedback(self, context, tool_name, result):
        if result.metadata.get("rejection_reason") == "role_not_allowed" or (
            result.status == "rejected" and tool_name in {"run_shell", "sandbox_test"}
        ):
            return result.content + (
                "\n【停】patcher 无权限调用此工具。请改用 read_file → apply_patch → quick_test；"
                "需要扩锁时用 expand_lock。"
            )
        return result.content

    def has_progress(self, context, tool_name, result):
        return bool(result.changed_files) or (
            tool_name in {"write_file", "patch_file", "apply_patch", "run_shell"}
            and result.status == "success"
        )

    def set_recovery(self, context, kind: str, directive: str, allowed_tools: set[str]) -> None:
        context.state.recovery_kind = str(kind)
        context.state.recovery_directive = str(directive)
        context.state.recovery_allowed_tools = set(allowed_tools)
        context.state.action_required = True

    def grounded(self, context) -> bool:
        """Return whether this Patcher has read an editable implementation path."""
        runtime = context.agent.session.get("_patcher_runtime", {}) or {}
        if bool(runtime.get("grounded")):
            return True
        lock = context.agent.tool_context.edit_lock
        return bool(lock is not None and lock.grounded_paths())

    def sync_grounding(self, context, tool_name: str, tool_args: dict, result) -> None:
        """Reflect implementation-read evidence into L1 state and action gating."""
        if result.status != "success" or tool_name != "read_file":
            return
        lock = context.agent.tool_context.edit_lock
        if lock is None:
            return
        path = str(tool_args.get("path") or "")
        lock.mark_read(path, auto_allow_impl=True)
        grounded_paths = lock.grounded_paths()
        if not grounded_paths:
            return
        runtime = context.agent.session.setdefault("_patcher_runtime", {})
        runtime["grounded"] = True
        runtime["grounded_paths"] = grounded_paths[:12]
        runtime["patch_required"] = True
        sink = context.agent.tool_context.grounding_sink
        if sink is not None:
            ledger = ((context.agent.session.get("memory") or {}).get("working") or {}).get(
                "evidence_ledger", []
            )
            sink(
                sorted(lock.allowed_edit),
                grounded_paths[:12],
                [dict(item) for item in ledger[-12:]] if isinstance(ledger, list) else [],
            )
        self.set_recovery(
            context,
            "grounded_evidence",
            "已读取实现文件并获得可编辑证据。停止继续探索，立即调用 apply_patch/patch_file；"
            "若确实无法形成补丁，只能声明 cannot_patch 并说明具体原因。",
            {"apply_patch", "patch_file", "finish_repair"},
        )
        context.emit(
            "patcher_grounded",
            {"path": path, "grounded_paths": grounded_paths[:12], "patch_required": True},
        )

    def review_result(self, context, tool_name: str, tool_args: dict, result) -> bool:
        """Reject needs_more_context after implementation evidence exists."""
        if tool_name != "finish_repair" or not self.grounded(
            context,
        ):
            return False
        status = str(tool_args.get("status") or "").strip().lower()
        if status != "needs_more_context":
            return False
        result.status = "rejected"
        result.error_code = "grounded_finish_blocked"
        result.retryable = False
        result.content = (
            "Error: 已有实现文件证据，不能以 needs_more_context 结束。"
            "请调用 apply_patch/patch_file；若无法修复请改用 cannot_patch。"
        )
        result.metadata.update({"required_next_action": "apply_patch_or_cannot_patch"})
        self.set_recovery(
            context,
            "grounded_finish_blocked",
            "已有实现文件证据，needs_more_context 已拒绝。请直接提交补丁，或声明 cannot_patch。",
            {"apply_patch", "patch_file", "finish_repair"},
        )
        context.emit("grounded_finish_blocked", {"status": status})
        return True

    def visible_tools(
        self, context, *, action_required: bool = False, strict_recovery: bool = False
    ) -> set[str] | None:
        """Project tool schemas to the current repair phase."""
        if action_required:
            names = {"apply_patch", "patch_file", "finish_repair"}
            if context.state.recovery_allowed_tools is not None:
                # Recovery directives are stricter than the normal convergence
                # window.  In particular stale writes expose only the exact
                # reread, while malformed writes expose apply_patch.
                return set(context.state.recovery_allowed_tools)
            # Patcher owns localization.  Once convergence has requested a
            # write, retain a small bounded read window so a newly discovered
            # implementation path is not made unreachable by schema gating.
            if not strict_recovery and context.guard.localization_reads_available:
                names.update(
                    {
                        "read_file",
                        "grep",
                        "list_files",
                        "ast_parse",
                        "code_lookup",
                        "code_relations",
                        "inspect_file",
                        "find_test",
                    }
                )
            # A truncated response is an action boundary.  Only stale
            # preimage recovery may open one explicitly bounded reread.
            elif not strict_recovery and context.has_targeted_read_reserve():
                names.update({"read_file", "ast_parse", "inspect_file"})
            return names
        if context.guard.phase != "converge":
            return None
        writes = {
            "write_file",
            "patch_file",
            "apply_patch",
            "finish_repair",
            "expand_lock",
            "quick_test",
        }
        quota = getattr(context.agent, "quota", None)
        reserves = list((quota.quota_summary() if quota else {}).get("read_reserves") or [])
        has_post_lock = any(item.get("kind") == "post_lock" for item in reserves)
        if has_post_lock or (
            context.guard.targeted_read_available and not context.state.action_required
        ):
            writes.add("read_file")
        return writes

    def enter_convergence(self, context, reason: str, *, step: int) -> None:
        context.emit(
            "convergence_gate_entered",
            {
                "step": step,
                "reason": reason,
                "reads_since_write": context.guard.reads_since_write,
            },
        )
        context.grant_read_reserve("*", kind="targeted", step=step)

    def preflight(self, context, tool_name, tool_args, *, step):
        quota_summary = (
            context.agent.quota.quota_summary()
            if hasattr(getattr(context.agent, "quota", None), "quota_summary")
            else {}
        )
        read_budget = (quota_summary.get("groups") or {}).get("read") or {}
        remaining = read_budget.get("remaining")
        if remaining is not None and int(remaining) <= 2:
            if context.guard.enter_convergence("read_budget_low"):
                self.enter_convergence(context, "read_budget_low", step=step)
        phase_before = context.guard.phase
        read_reservation = context.matching_read_reservation(tool_name, tool_args)
        preflight = context.guard.preflight(
            tool_name,
            tool_args,
            read_reservation=read_reservation,
        )
        if phase_before == "explore" and context.guard.phase == "converge":
            self.enter_convergence(
                context, context.guard.convergence_reason or "duplicate_read", step=step
            )
        if preflight is not None and preflight.action == "allow_targeted_read":
            context.grant_read_reserve("*", kind="targeted", step=step)
        elif preflight is not None and preflight.action == "allow_reserved_read":
            pass
        elif preflight is not None and preflight.action.startswith("block_"):
            event = (
                "duplicate_read_blocked"
                if preflight.action == "block_duplicate_read"
                else "convergence_read_blocked"
            )
            context.emit(
                event,
                {
                    "step": step,
                    "tool": tool_name,
                    "path": str(tool_args.get("path") or ""),
                    "phase": context.guard.phase,
                },
            )
            context.state.blocked_convergence_reads += 1
            if context.state.blocked_convergence_reads >= 2:
                context.state.action_required = True
                context.emit(
                    "action_required",
                    {
                        "step": step,
                        "blocked_read_attempts": context.state.blocked_convergence_reads,
                        "allowed_actions": [
                            "apply_patch",
                            "patch_file",
                            "expand_lock",
                            "terminal",
                        ],
                    },
                )
            result = ToolResult(
                content=(
                    f"Error: {preflight.detail}。{preflight.replan_hint} "
                    "可用动作: apply_patch/patch_file/expand_lock/终止。"
                ),
                status="rejected",
                error_code="convergence_required",
                metadata={},
                retryable=False,
            )
            self.set_recovery(
                context,
                "convergence_required",
                "读取请求被收敛闸门拒绝。请停止重复读取，直接调用 apply_patch/patch_file，"
                "或调用 finish_repair 说明证据不足。",
                {"apply_patch", "patch_file", "finish_repair"},
            )
            return result
        return None

    def recover_result(self, context, tool_name, tool_args, result, *, step):
        """Convert a rejected patch into a bounded, evidence-directed recovery action."""
        error_code = result.error_code
        target_paths = tool_target_paths(tool_name, tool_args)
        path_hint = target_paths[0] if target_paths else ""
        if error_code == "stale_preimage":
            if context.guard.request_targeted_reread("stale_preimage"):
                for target_path in target_paths:
                    context.grant_read_reserve(target_path, kind="targeted", step=step)
            self.set_recovery(
                context,
                "stale_preimage",
                f"补丁的旧文本已失效。先对 {', '.join(target_paths) or '目标文件'} "
                "执行一次精确 read_file，"
                "再基于刚读到的上下文调用 apply_patch/patch_file；不要重复旧补丁。",
                {"read_file", "finish_repair"}
                if context.has_targeted_read_reserve()
                else {"apply_patch", "finish_repair"},
            )
            context.emit(
                "stale_patch_rejected",
                {
                    "step": step,
                    "tool": tool_name,
                    "path": path_hint,
                    "paths": target_paths,
                    "current_sha256": result.metadata.get("current_sha256", ""),
                    "recovery_action": "targeted_reread_then_retry",
                },
            )
        elif error_code == "invalid_args":
            self.set_recovery(
                context,
                "invalid_args",
                "写入参数无效。禁止空 old_text/new_text 或重复相同工具调用；"
                "请改用包含文件路径、上下文行和 +/- 行的 apply_patch，"
                "或调用 finish_repair 说明无法修复。",
                {"apply_patch", "finish_repair"},
            )
            context.emit(
                "patch_write_rejected",
                {"step": step, "tool": tool_name, "error_code": error_code},
            )
        elif error_code == "no_change":
            context.guard.request_targeted_reread("no_change")
            for target_path in target_paths:
                context.grant_read_reserve(target_path, kind="targeted", step=step)
            self.set_recovery(
                context,
                "no_change",
                f"上一次写入没有产生磁盘变化。先精确读取 "
                f"{', '.join(target_paths) or '目标文件'} 的当前内容，"
                "再提交不同的 apply_patch，或调用 finish_repair。",
                {"read_file", "finish_repair"},
            )
            if context.task_state is not None:
                context.task_state.node_timings["patch_no_change"] = True
            context.emit(
                "patch_no_change",
                {
                    "step": step,
                    "tool": tool_name,
                    "recovery_action": "reread_then_retry_or_finish",
                },
            )
        elif error_code == "edit_lint_reject":
            self.set_recovery(
                context,
                "edit_lint_reject",
                "补丁因编辑期语法检查未落盘。请修正语法后用 apply_patch 提交，不要重复相同内容。",
                {"apply_patch", "finish_repair"},
            )
            context.emit(
                "patch_write_rejected",
                {"step": step, "tool": tool_name, "error_code": error_code},
            )

    def on_success(self, context, tool_name, tool_args, result, *, step):
        if tool_name in {"write_file", "patch_file", "apply_patch"}:
            context.state.action_required = False
            context.state.recovery_directive = ""
            context.state.recovery_allowed_tools = None
            context.state.recovery_kind = ""
        consumed_reserve = result.metadata.get("read_reserve_consumed")
        if isinstance(consumed_reserve, dict):
            context.emit(
                "post_lock_read_consumed"
                if consumed_reserve.get("kind") == "post_lock"
                else "targeted_read_consumed",
                {"step": step, **consumed_reserve},
            )
            if (
                tool_name == "read_file"
                and consumed_reserve.get("kind") == "targeted"
                and context.state.recovery_kind in {"stale_preimage", "no_change"}
            ):
                self.set_recovery(
                    context,
                    "post_reread",
                    "精确重读已完成。现在必须基于该读取结果调用 apply_patch/patch_file，"
                    "或调用 finish_repair；不要再次读取同一范围。",
                    {"apply_patch", "patch_file", "finish_repair"},
                )
        if tool_name == "expand_lock" and tool_args.get("path") and "expanded:" in result.content:
            generation = 0
            lock = context.agent.tool_context.edit_lock
            if lock is not None:
                generation = lock.required_read_generation(str(tool_args["path"]))
            context.grant_read_reserve(
                str(tool_args["path"]).replace("\\", "/"),
                kind="post_lock",
                step=step,
                generation=generation,
            )

    def recovery_anchors(self, text: str, *, max_chars: int = 6000) -> str:
        """Keep bounded source/target anchors when a model turn is truncated."""
        raw = str(text or "")
        if not raw:
            return ""
        lines = raw.splitlines()
        markers = (
            "allowed_edit:",
            "DISK GROUNDING",
            "嫌疑位置",
            "相关测试文件",
            "失败面",
            "[PATCHER RUNTIME CONTRACT]",
        )
        starts = [
            index for index, line in enumerate(lines) if any(marker in line for marker in markers)
        ]
        chunks: list[str] = []
        used = 0
        for start in starts:
            chunk = "\n".join(lines[start : start + 36]).strip()
            if not chunk:
                continue
            remaining = max_chars - used
            if remaining <= 0:
                break
            if len(chunk) > remaining:
                chunk = chunk[:remaining].rstrip() + "\n... anchors truncated ..."
            chunks.append(chunk)
            used += len(chunk)
        return "\n\n".join(chunks)
