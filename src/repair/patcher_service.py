from __future__ import annotations

import time
from pathlib import Path

from agent_runtime.logging_setup import get_logger
from src.prompts.loader import load_role_prompt
from src.prompts.patcher_task_builder import assemble_patcher_variables
from src.prompts.repair_tasks import render_repair_task
from src.repair.prompt_router import patcher_variant_for
from src.state import CandidatePatch, RepairPlan, RepairState, RetrievedContext, SuspectLocation

log = get_logger("orchestrator")
DEFAULT_REPAIR_TIMEOUT_S = 180


class PatcherService:
    def __init__(self, session):
        self.session = session

    @staticmethod
    def patcher_system_prompt(plan: RepairPlan | None) -> str:
        """Patcher system 文本（base + issue suffix）。

        工具环使用 Agent 构造时的 system prompt。
        """
        return load_role_prompt("patcher", patcher_variant_for(plan))

    @staticmethod
    def inject_repair_task_summary(agent, state: RepairState) -> None:
        """将 _parse_issue 的结构化摘要注入 Agent working memory。

        供 L1 memory 检索（§4.4）使用。
        """
        plan = state.repair_plan
        if plan is None:
            return
        from agent_runtime.features.memory import set_task_summary

        parts = [f"[{plan.issue_type}]"]
        if plan.reasoning:
            parts.append(plan.reasoning[:200])
        if plan.suspect_files:
            parts.append(f"files: {', '.join(plan.suspect_files[:3])}")
        summary = " ".join(parts)
        set_task_summary(agent.session["memory"], summary)

    def run_agent(
        self,
        agent,
        prompt: str,
        agent_name: str,
        state: RepairState | None = None,
        *,
        l2_phase: str = "",
        l2_attempt: int = 0,
    ) -> tuple[str, dict]:
        """执行 Agent 调用（Verifier 用，保留 Agent loop）。"""
        from agent_runtime.log_context import log_context

        if agent is not None and state is not None and (state.repair_plan is not None):
            self.inject_repair_task_summary(agent, state)
        t0 = time.time()
        run_id = getattr(agent, "shared_run_id", None)
        task_id = ""
        if state is not None and l2_phase:
            task_id = self.session.asks.begin_l2_agent_ask(
                state, agent, agent_name=agent_name, phase=l2_phase, attempt=l2_attempt
            )
        try:
            with log_context(run_id=run_id, agent=agent_name):
                answer = agent.ask(prompt, skip_plan=True)
        except Exception as e:
            if state is not None:
                state.agent_errors[agent_name] = str(e)
            log.warning("[%s] Agent 失败: %s", agent_name, e)
            elapsed_ms = int((time.time() - t0) * 1000)
            if state is not None and task_id:
                self.session.asks.finish_l2_agent_ask(
                    state,
                    agent,
                    agent_name=agent_name,
                    phase=l2_phase,
                    attempt=l2_attempt,
                    task_id=task_id,
                    elapsed_ms=elapsed_ms,
                    stop_reason="error",
                )
            return ("", {"total_ms": elapsed_ms, "internal": {}})
        elapsed_ms = int((time.time() - t0) * 1000)
        internal = getattr(agent, "last_run_node_timings", None) or {}
        last_ts = getattr(agent, "last_task_state", None)
        if state is not None and task_id:
            self.session.asks.finish_l2_agent_ask(
                state,
                agent,
                agent_name=agent_name,
                phase=l2_phase,
                attempt=l2_attempt,
                task_id=task_id,
                elapsed_ms=elapsed_ms,
                stop_reason=getattr(last_ts, "stop_reason", "") if last_ts else "",
                tool_steps=getattr(last_ts, "tool_steps", 0) if last_ts else 0,
            )
        return (answer, {"total_ms": elapsed_ms, "internal": dict(internal)})

    def run_patcher(self, state: RepairState) -> tuple[list[CandidatePatch], dict]:
        """Run the sole patch path: governed tools mutate disk, Runtime snapshots the diff."""
        plan = state.repair_plan
        self.session.evidence.merge_blackboard_for_patch(state)
        feedback = state.feedback or ""
        from src.repair.execution.control import pop_patcher_depth, push_patcher_depth
        from src.repair.execution.patcher_contract import (
            begin_patcher_attempt,
            classify_patcher_attempt,
            record_patcher_terminal_status,
            render_patcher_runtime_contract,
        )
        from src.repair.verification.fail_surface import (
            build_fail_surface,
            build_fail_surface_prompt_block,
            build_verify_feedback_payload,
            render_verify_feedback_block,
        )

        begin_patcher_attempt(state)
        surface = build_fail_surface(
            state, repo_root=self.session.repo_root, sandbox_context=self.session.sandbox_context
        )
        bucket = str(state.control.verify_bucket or "")
        fail_block = build_fail_surface_prompt_block(surface, bucket=bucket)
        if fail_block:
            state.node_timings["fail_surface_target"] = surface.verify_target
            state.node_timings["fail_surface_nodeids"] = list(surface.nodeids)
            feedback = f"{fail_block}\n\n{feedback}".strip() if feedback else fail_block
        structured_feedback = state.control.structured_verify_feedback
        if not isinstance(structured_feedback, dict) and state.verification_result is not None:
            payload = build_verify_feedback_payload(
                state,
                repo_root=self.session.repo_root,
                result=state.verification_result,
                sandbox_context=self.session.sandbox_context,
            )
            state.control.structured_verify_feedback = payload.to_dict()
            structured_feedback = payload.to_dict()
        if isinstance(structured_feedback, dict):
            feedback_block = render_verify_feedback_block(structured_feedback)
            if feedback_block and feedback_block not in feedback:
                feedback = f"{feedback_block}\n\n{feedback}".strip() if feedback else feedback_block
        allowed = list(state.control.allowed_edit or [])
        runtime_bits = [
            "【repair runtime】read → apply_patch（*** Begin/End Patch，"
            "Update 须含原文上下文）→ quick_test。",
            "可 expand_lock；不要用 run_shell/sandbox_test（无权限）。真源=磁盘 diff。",
            "expand_lock operation=modify|create|delete；新增文件用 create 授权后 Add File，"
            "修改和删除须重新读取。",
        ]
        if allowed:
            runtime_bits.append("allowed_edit: " + ", ".join(allowed[:12]))
        block = "\n".join(runtime_bits)
        feedback = f"{block}\n\n{feedback}".strip() if feedback else block
        suspects_for_prompt = state.suspect_locations
        patch_steps = self.session.patcher.config.max_steps
        depth_token = push_patcher_depth(self.session.patcher, patch_steps)
        state.node_timings["patch_steps"] = patch_steps
        prompt, tpl_meta = self.patcher_prompt(
            suspects_for_prompt,
            state.retrieved_context,
            feedback,
            plan=plan,
            issue=state.issue_input,
            runtime_contract_block=render_patcher_runtime_contract(state),
            allowed_edit=allowed,
        )
        try:
            applied, meta = self.run_patcher_toolized(state, prompt, tpl_meta)
            status = classify_patcher_attempt(
                state,
                applied,
                apply_failed=bool(state.agent_errors.get("patcher_apply")),
                terminal_answer=str(meta.get("agent_answer") or ""),
                agent_stop_reason=str(meta.get("agent_stop_reason") or ""),
            )
            record_patcher_terminal_status(state, status, reason="patcher_turn_complete", meta=meta)
            return (applied, meta)
        finally:
            pop_patcher_depth(self.session.patcher, depth_token)

    def compact_patcher_history(self, state) -> None:
        """CC 序 compact；thrash 时记停损标志。"""
        if self.session.patcher is None:
            return
        from src.repair.context_compact import compact_tool_history

        session = getattr(self.session.patcher, "session", None) or {}
        hist = list(session.get("history") or [])
        if not hist:
            return
        prior = int(state.node_timings.get("compact_count") or 0)
        result = compact_tool_history(hist, prior_compacts=prior)
        session["history"] = result.messages
        state.node_timings["compact_count"] = result.compact_count
        if result.dropped_chars:
            state.node_timings["compact_dropped_chars"] = (
                int(state.node_timings.get("compact_dropped_chars") or 0) + result.dropped_chars
            )
        if result.thrash:
            n = (
                int(state.node_timings.get("compact_thrash_count") or 0)
                + result.compact_thrash_count
            )
            state.node_timings["compact_thrash_count"] = n
            tip = "compact_thrash：上下文压缩后仍爆满，停止空转 compact，请缩小改动面。"
            state.feedback = f"{state.feedback}\n{tip}".strip() if state.feedback else tip
            log.warning("[compact] thrash stop-loss")

    def run_patcher_toolized(
        self, state: RepairState, prompt: str, tpl_meta: dict
    ) -> tuple[list[CandidatePatch], dict]:
        from agent_runtime.cancellation import CancelledError
        from agent_runtime.run_coordination import CoordinationError
        from src.repair.plan_binding import RepairPlanBinding

        try:
            if self.session.plan_binding is None:
                self.session.plan_binding = RepairPlanBinding(self.session, state)
            return self.session.plan_binding.run_patcher(
                prompt, tpl_meta, lambda p, m: self.run_patcher_agent(state, p, m)
            )
        except CancelledError:
            return ([], {"user_cancel": True, "edit_mode": "cancelled", "total_ms": 0})
        except (ValueError, OSError, CoordinationError) as exc:
            if self.session.lifecycle.is_repair_cancelled():
                return ([], {"user_cancel": True, "edit_mode": "cancelled", "total_ms": 0})
            state.agent_errors["plan_runtime"] = str(exc)
            state.control.plan_blocked = True
            return (
                [],
                {
                    "total_ms": 0,
                    "model_call_ms": 0,
                    "parse_apply_ms": 0,
                    "plan_blocked": str(exc),
                    "edit_mode": "plan_blocked",
                },
            )

    def run_patcher_agent(
        self, state: RepairState, prompt: str, tpl_meta: dict
    ) -> tuple[list[CandidatePatch], dict]:
        """Agent loop：工具改盘 → 快照 diff 成 CandidatePatch（不再二次 apply）。"""
        from src.repair.execution.edit_from_disk import patches_from_snapshot_diff
        from src.repair.failure_tags import check_patch_faithfulness

        t_start = time.time()
        compact_enabled = self.session.repair_config.patcher_compact
        if compact_enabled:
            self.compact_patcher_history(state)
        before = self.session.transaction.snapshot_repo()
        observation_start = len(
            (getattr(self.session.patcher, "session", {}) or {}).get("tool_observations", [])
        )
        answer, agent_timing = self.run_agent(
            self.session.patcher,
            prompt,
            "patcher",
            state,
            l2_phase="patch",
            l2_attempt=state.retry_count,
        )
        if (
            self.session.plan_binding is not None
            and self.session.plan_binding.exploration is not None
        ):
            self.session.plan_binding.exploration.drain(cancel=True)
        context = self.session.patcher.tool_context
        if context.execution_uncertain or context.sandbox_uncertain:
            state.control.coordination_status = "recovery_required"
            state.set_status("recovery_required", "patcher_write_unconfirmed")
            total_ms = int((time.time() - t_start) * 1000)
            return (
                [],
                {
                    "total_ms": total_ms,
                    "model_call_ms": int(agent_timing.get("total_ms") or total_ms),
                    "parse_apply_ms": 0,
                    "edit_mode": "recovery_required",
                    "execution_uncertain": True,
                },
            )
        after = self.session.transaction.snapshot_repo()
        observations = list(
            (getattr(self.session.patcher, "session", {}) or {}).get("tool_observations", [])
        )[observation_start:]
        write_tools = {"write_file", "patch_file", "apply_patch"}
        write_attempted = any(
            str(item.get("tool_name") or "") in write_tools for item in observations
        )
        state.control.patcher_write_attempted = bool(write_attempted)
        lock = getattr(self.session, "edit_lock", None)
        if lock is not None:
            state.node_timings["apply_patch_ok_count"] = int(
                getattr(lock, "apply_patch_ok_count", 0) or 0
            )
            state.node_timings["edit_lint_reject_count"] = int(
                getattr(lock, "edit_lint_reject_count", 0) or 0
            )
            state.node_timings["unread_write_reject_count"] = int(
                getattr(lock, "unread_write_reject_count", 0) or 0
            )
            state.node_timings["apply_path_reject_count"] = int(
                getattr(lock, "apply_path_reject_count", 0) or 0
            )
            if hasattr(self.session.events, "progress_emitter"):
                summary = (
                    f"apply_ok={lock.apply_patch_ok_count} lint_rej={lock.edit_lint_reject_count}"
                )
                self.session.events.progress_emitter().emit("tool_progress", summary=summary)
                if hasattr(self.session.events, "emit_repair_span"):
                    self.session.events.emit_repair_span(
                        "apply_patch_span",
                        {"summary": summary, "apply_patch_ok_count": lock.apply_patch_ok_count},
                    )
        patches = patches_from_snapshot_diff(before, after)
        if write_attempted and (not patches):
            if lock is not None and (
                getattr(lock, "unread_write_reject_count", 0)
                or getattr(lock, "apply_path_reject_count", 0)
                or getattr(lock, "edit_lint_reject_count", 0)
            ):
                state.control.patcher_write_rejected = True
            else:
                state.control.patcher_export_failed = True
        patches, rejected = check_patch_faithfulness(
            patches, state, soft_keep=False, repo_root=str(self.session.repo_root or "")
        )
        if rejected:
            state.agent_errors["patcher_hallucinated_file"] = rejected[0]
            for rel in rejected:
                if rel in before:
                    target = Path(self.session.repo_root) / rel
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(before[rel], encoding="utf-8")
                    except OSError as e:
                        log.warning("[patcher] restore rejected file %s: %s", rel, e)
            after = self.session.transaction.snapshot_repo()
            patches = patches_from_snapshot_diff(before, after)
            patches, _ = check_patch_faithfulness(
                patches, state, soft_keep=False, repo_root=str(self.session.repo_root or "")
            )
        total_ms = int((time.time() - t_start) * 1000)
        model_call_ms = int(agent_timing.get("total_ms") or total_ms)
        meta = {
            "model_call_ms": model_call_ms,
            "parse_apply_ms": max(0, total_ms - model_call_ms),
            "total_ms": total_ms,
            "edit_mode": "tools",
            "internal": agent_timing.get("internal") or {},
            "task_template_source": (tpl_meta or {}).get("task_template_source"),
            "agent_answer": str(answer or "")[:4000],
            "agent_stop_reason": str(
                getattr(getattr(self.session.patcher, "last_task_state", None), "stop_reason", "")
                or ""
            ),
        }
        if not patches:
            state.node_timings["patcher_edit_mode"] = "tools_empty"
            return ([], meta)
        state.node_timings["patcher_edit_mode"] = "tools"
        self.session.last_apply_errors = []
        self.session.last_sibling_warnings = []
        log.info("[patcher] toolized 落盘 %d 个文件", len(patches))
        return (patches, meta)

    def build_feedback(self, result, *, state=None):
        from src.repair.feedback_service import RepairFeedbackService

        ctx = self.session.repair_ctx
        tracer = ctx.repair_tracer if ctx else None
        return RepairFeedbackService(
            self.session.repo_root, self.session.sandbox_context, tracer.emit if tracer else None
        ).build(result, state=state)

    def emit_skill_hint_trace(self, agent: str, render) -> None:
        from src.skills.skill_block import skill_hint_rendered_trace

        tracer = self.session.repair_ctx.repair_tracer if self.session.repair_ctx else None
        if tracer is None or not render.text:
            return
        tracer.emit(agent, "skill_hint_rendered", skill_hint_rendered_trace(render))

    def patcher_prompt(
        self,
        suspects: list[SuspectLocation],
        context: RetrievedContext | None,
        feedback: str = "",
        plan: RepairPlan | None = None,
        issue: str = "",
        runtime_contract_block: str = "",
        allowed_edit: list[str] | None = None,
    ) -> tuple[str, dict]:
        ctx = self.session.repair_ctx
        blackboard = ctx.blackboard if ctx is not None else None
        evidence_block = ""
        if self.session.patcher is not None:
            try:
                from agent_runtime.features.memory import render_evidence_ledger

                memory = (
                    self.session.patcher.session.get("memory", {})
                    if getattr(self.session.patcher, "session", None)
                    else {}
                )
                evidence_block = render_evidence_ledger(memory)
            except Exception:
                evidence_block = ""
        variables, render, subscribe_meta = assemble_patcher_variables(
            suspects=suspects,
            context=context,
            feedback=feedback,
            evidence_block=evidence_block,
            plan=plan,
            issue=issue,
            read_snippet=self.session.exploration.read_code_snippet,
            read_test_context=self.session.exploration.read_test_context,
            fallback_suspects=self.session.exploration.fallback_suspects_from_plan,
            blackboard=blackboard,
            diff_only=True,
            read_line_range=self.session.exploration.read_line_range,
            runtime_contract_block=runtime_contract_block,
            allowed_edit=allowed_edit,
        )
        tracer = ctx.repair_tracer if ctx is not None else None
        if subscribe_meta and tracer is not None:
            tracer.emit("orchestrator", "blackboard_prefix_subscribed", subscribe_meta)
        self.emit_skill_hint_trace("patcher", render)
        return render_repair_task("patcher", variables)
