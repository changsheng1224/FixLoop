"""Orchestrator：纯 Python 编排器（不调 LLM）。

工作流：Issue 解析与规则种子 → Patcher 工具环 → Critic → Verifier 回灌。
"""

import threading
import time
from pathlib import Path

from agent_runtime.logging_setup import get_logger
from src.prompts.loader import load_role_prompt
from src.prompts.patcher_task_builder import assemble_patcher_variables
from src.prompts.repair_tasks import render_repair_task
from src.repair.execution.lifecycle import ExecutionLifecycleMixin
from src.repair.execution.patch_applier import apply_patch_to_text
from src.repair.execution.transaction import PatchTransactionMixin
from src.repair.output_parsers import (
    parse_verification,
)
from src.repair.pipeline import RepairPipelineMixin
from src.repair.prompt_router import (
    patcher_variant_for,
)
from src.repair.run_context import RepairRunContext
from src.repair.verification.service import VerificationMixin
from src.state import (
    CandidatePatch,
    RepairPlan,
    RepairState,
    RetrievedContext,
    SuspectLocation,
    VerificationResult,
)

log = get_logger("orchestrator")

__all__ = ["Orchestrator", "apply_patch_to_text"]

DEFAULT_REPAIR_TIMEOUT_S = 180


class Orchestrator(
    ExecutionLifecycleMixin, PatchTransactionMixin, VerificationMixin, RepairPipelineMixin
):
    """纯 Python 修复编排器。

    不调 LLM，只做调度和状态管理。
    """

    def __init__(
        self,
        patcher,
        verifier=None,
        *,
        use_pytest_verify: bool = False,
        require_sandbox: bool = False,
        sandbox_policy: str = "",
        allow_static_verify_fallback: bool = False,
        l1_prompt_cache_key: str = "",
        sandbox_context=None,
        sandbox_task_spec=None,
        sandbox_backend_factory=None,
        workspace_policy: str = "provided",
        execution_profile: str = "",
        execution_tier: str = "",
        workspace_state_base: str = "",
        project_root: str = "",
        repair_config=None,
    ):
        self.patcher = patcher
        self.verifier = verifier
        self.use_pytest_verify = use_pytest_verify
        self.execution_tier = execution_tier
        self._verify_service = None
        from src.repair.config import load_repair_config

        self.repair_config = repair_config or load_repair_config(
            workspace_root=getattr(patcher, "cwd", None),
            cli_overrides={"sandbox_policy": "required" if require_sandbox else sandbox_policy}
            if require_sandbox or sandbox_policy
            else None,
        )
        configured_policy = self.repair_config.sandbox_policy
        self.sandbox_policy = configured_policy
        self.require_sandbox = bool(require_sandbox or configured_policy == "required")
        self.allow_static_verify_fallback = allow_static_verify_fallback
        from src.repair.critic import resolve_critic_mode
        from src.repair.verification.execution_policy import resolve_execution_policy

        self.critic_mode = resolve_critic_mode()
        self.execution_policy = resolve_execution_policy(
            sandbox_policy=configured_policy,
            execution_tier=execution_tier,
            require_sandbox=require_sandbox,
            sandbox=sandbox_context is not None,
            verifier=verifier,
            host=use_pytest_verify,
            static=allow_static_verify_fallback,
        )
        if sandbox_context is not None and (
            verifier is not None
            or use_pytest_verify
            or allow_static_verify_fallback
            or require_sandbox
        ):
            raise ValueError("wsl_bwrap cannot mix with Docker/host/static verification")
        if (
            sandbox_context is not None
            and getattr(patcher, "tool_context", None) is not sandbox_context
        ):
            raise ValueError("wsl_bwrap patcher and verifier must share ToolContext")
        self._sandbox_context = sandbox_context
        if workspace_policy not in {"worktree", "provided"}:
            raise ValueError("unknown workspace policy")
        self._workspace_policy = workspace_policy
        self._execution_profile = execution_profile or (
            f"{'wsl_bwrap' if sandbox_context is not None else 'legacy'}:"
            f"pytest={bool(use_pytest_verify)}:static={bool(allow_static_verify_fallback)}"
        )
        self._workspace_state_base = workspace_state_base
        self._project_root = project_root or getattr(sandbox_task_spec, "project_root", "")
        self._sandbox_task_spec = sandbox_task_spec
        self._sandbox_backend_factory = sandbox_backend_factory
        self._sandbox_worktree_handle = None
        self._sandbox_backend_close_attempted = False
        if sandbox_task_spec is not None and (
            sandbox_context is None or sandbox_backend_factory is None
        ):
            raise ValueError("sandbox task requires context and a binding-aware backend factory")
        if sandbox_context is not None and sandbox_task_spec is None:
            sandbox_context.sandbox_identity = {
                "backend": "wsl_bwrap",
                "policy_digest": "",
                "distribution_id": "wsl2",
                "mapping_id": str(getattr(sandbox_context, "root", "") or ""),
                "receipt_id": "",
                "receipt_checksum": "",
            }
        self.l1_prompt_cache_key = l1_prompt_cache_key or self._resolve_l1_prompt_cache_key()
        self._repair_ctx: RepairRunContext | None = None
        self._collaboration_runtime = None
        self._plan_binding = None
        self._log_run_id_token = None
        # 修复目标目录：优先 --repo / Agent cwd，而非 git 顶层仓库
        self._repo_root = str(Path.cwd())
        for agent in self._active_agents:
            workspace = getattr(agent, "workspace", None)
            candidate = (
                getattr(agent, "_cwd", "")
                or getattr(workspace, "cwd", "")
                or getattr(workspace, "repo_root", "")
            )
            if candidate:
                self._repo_root = candidate
                break
        self._sandbox_repository_root = self._repo_root
        self._repair_gateways = self._collect_repair_gateways(
            *self._active_agents,
        )

    @property
    def _active_agents(self) -> tuple:
        """Return configured repair agents without repeating null filtering."""
        return (self.patcher,) if self.patcher is not None else ()

    @staticmethod
    def _collect_repair_gateways(*agents) -> tuple:
        gateways = []
        seen: set[int] = set()
        for agent in agents:
            gw = getattr(agent, "_repair_gateway", None)
            if gw is None:
                continue
            marker = id(gw)
            if marker in seen:
                continue
            seen.add(marker)
            gateways.append(gw)
        return tuple(gateways)

    @staticmethod
    def _resolve_l1_prompt_cache_key_from_agents(*agents) -> str:
        hashes = []
        for agent in agents:
            if agent is None:
                continue
            prefix = getattr(agent, "_prefix", None)
            if prefix is not None and getattr(prefix, "hash", ""):
                hashes.append(prefix.hash)
        if hashes and len(set(hashes)) == 1:
            return hashes[0]
        return ""

    def _resolve_l1_prompt_cache_key(self) -> str:
        return self._resolve_l1_prompt_cache_key_from_agents(*self._active_agents)

    def _set_collaboration_context(self, state: RepairState) -> None:
        """Pass current mode/phase to gateways when the runtime owns the state."""
        # Governance applies to the active patcher and verifier roles.
        harness = getattr(self._repair_ctx, "harness_control", None)
        control_mode = getattr(getattr(harness, "control_mode", None), "value", "") or (
            state.human_control or {}
        ).get("mode", "auto")
        approved_tools = set((state.human_control or {}).get("approved_tools", []) or [])
        for agent in self._active_agents:
            gateway = getattr(agent, "_tool_dispatch", None)
            if gateway is not None and hasattr(gateway, "set_context"):
                gateway.set_context(
                    getattr(agent, "_agent_name", "") or "agent",
                    mode=str(state.intent.get("mode", "repair")),
                    phase=str(state.phase),
                    evidence=bool(state.evidence or state.suspect_locations),
                    read_before_write=bool(state.control.allowed_edit or state.suspect_locations),
                    control_mode=control_mode,
                    approved_tools=approved_tools,
                )

    def _on_collaboration_phase(self, state: RepairState, phase: str, reason: str = "") -> None:
        """Synchronize policy context and role set at a runtime phase boundary."""
        from src.collaboration_governance import CollaborationGovernance, RoleLifecycle

        transition = state.transition_phase(phase, reason, allow_recovery=phase == "recovery")
        if not transition.valid:
            state.node_timings.setdefault("phase_transition_rejections", []).append(
                transition.to_dict()
            )
            return
        harness = getattr(self._repair_ctx, "harness_control", None)
        if harness is not None:
            harness.record_phase(
                phase,
                state_revision=int(getattr(state, "state_revision", 0) or 0),
                reason=reason,
            )
            if phase == "patch" and int(getattr(state, "retry_count", 0) or 0) >= harness.attempt:
                harness.retry(reason="repair retry")
        if phase == "patch":
            for role in ("verifier",):
                if role in state.active_roles:
                    event = CollaborationGovernance.lifecycle_transition(
                        state, role, RoleLifecycle.SUSPENDED, reason or "patch in progress"
                    )
                    self._emit_collaboration_event(event)
            role = "patcher"
            if getattr(self, role, None) is not None:
                event = CollaborationGovernance.lifecycle_transition(
                    state, role, RoleLifecycle.REACTIVATED, reason or "patch phase"
                )
                self._emit_collaboration_event(event)
        elif phase == "verify":
            if getattr(self, "patcher", None) is not None and "patcher" in state.active_roles:
                event = CollaborationGovernance.lifecycle_transition(
                    state, "patcher", RoleLifecycle.SUSPENDED, reason or "verification phase"
                )
                self._emit_collaboration_event(event)
            if getattr(self, "verifier", None) is not None:
                event = CollaborationGovernance.lifecycle_transition(
                    state, "verifier", RoleLifecycle.REACTIVATED, reason or "verification phase"
                )
                self._emit_collaboration_event(event)
        elif phase in {"done", "failed"}:
            for role in list(state.role_lifecycle):
                event = CollaborationGovernance.lifecycle_transition(
                    state, role, RoleLifecycle.COMPLETED, reason or "repair finalized"
                )
                self._emit_collaboration_event(event)
        runtime = getattr(self, "_collaboration_runtime", None)
        if runtime is not None:
            try:
                runtime.advance(
                    phase,
                    state,
                    terminal_status=str(getattr(state, "status", "") or ""),
                )
            except Exception as exc:
                state.agent_errors.setdefault("collaboration_runtime", str(exc)[:500])
        self._set_collaboration_context(state)

    def _emit_collaboration_event(self, event: dict) -> None:
        ctx = getattr(self, "_repair_ctx", None)
        tracer = getattr(ctx, "repair_tracer", None) if ctx else None
        if tracer is not None:
            tracer.emit("orchestrator", "agent_set_changed", event)

    def _on_harness_event(self, state: RepairState, event: dict) -> None:
        """Persist one Harness event in state and the canonical repair trace."""
        events = list(getattr(state, "harness_events", []) or [])
        events.append(dict(event))
        state.harness_events = events[-2000:]
        control = getattr(self._repair_ctx, "harness_control", None)
        if control is not None:
            snapshot = control.snapshot()
            state.harness_control = {
                key: value for key, value in snapshot.items() if key not in {"events", "metrics"}
            }
            state.harness_metrics = dict(snapshot.get("metrics") or {})
            state.human_control = {
                **dict(getattr(state, "human_control", {}) or {}),
                "mode": snapshot.get("control_mode", "auto"),
            }
        tracer = getattr(self._repair_ctx, "repair_tracer", None)
        if tracer is not None:
            tracer.emit("harness", "harness_event", event, status=event.get("status") or None)

    def _issue_preparation(self):
        from src.repair.issue_preparation import IssuePreparationService

        return IssuePreparationService(
            self._repo_root,
            read_source=lambda path: self._read_line_range(path, 1, 10000),
            parse_line=self._parse_file_line,
            patcher=self.patcher,
            classify=self._classify_issue_type,
            light_client=getattr(self, "_light_client", None),
        )

    def _parse_issue(self, issue):
        from agent_runtime.intent.models import RouteContext

        ctx = self._repair_ctx
        tracer = ctx.repair_tracer if ctx else None
        plan, self._last_intent_result = self._issue_preparation().prepare(
            issue,
            RouteContext(
                channel="repair",
                light_client=getattr(self, "_light_client", None),
                emit=lambda name, payload: (
                    tracer.emit("orchestrator", name, payload) if tracer else None
                ),
                cancel_token=ctx.cancel_token if ctx else None,
                budget=getattr(self, "_model_budget", None),
                deadline=getattr(self, "_model_deadline", None),
                model_input_limit=self.patcher.config.input_window(384)
                if getattr(self, "_model_budget", None) is not None
                else None,
            ),
        )
        return plan

    @staticmethod
    def _classify_issue_type(issue):
        from src.repair.issue_preparation import IssuePreparationService

        return IssuePreparationService._classify_issue_type(issue)

    def _llm_classify_issue(self, issue):
        return self._issue_preparation().classify_with_model(issue)

    @staticmethod
    def _parse_file_line(issue, file_path):
        from src.repair.issue_preparation import IssuePreparationService

        return IssuePreparationService._parse_file_line(issue, file_path)

    def _fallback_suspects_from_plan(self, plan, issue):
        return self._issue_preparation()._fallback_suspects_from_plan(plan, issue)

    def _find_import_line_number(self, file_path):
        return self._issue_preparation()._find_import_line_number(file_path)

    def _match_skill(self, issue: str, *, language: str = "python"):
        """从 YAML Skill 文件中匹配 Issue 对应的修复策略。"""
        from src.skills.matcher import match_skill_semantic

        return match_skill_semantic(issue, language=language)

    def _verification_enabled(self) -> bool:
        return self.execution_policy.enabled

    @staticmethod
    def _patcher_system_prompt(plan: RepairPlan | None) -> str:
        """Patcher system 文本（base + issue suffix）。

        工具环使用 Agent 构造时的 system prompt。
        """
        return load_role_prompt("patcher", patcher_variant_for(plan))

    @staticmethod
    def _inject_repair_task_summary(agent, state: RepairState) -> None:
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

    def _run_agent(
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

        # 注入 repair 任务上下文到 Agent working memory
        if agent is not None and state is not None and state.repair_plan is not None:
            self._inject_repair_task_summary(agent, state)

        t0 = time.time()
        run_id = getattr(agent, "shared_run_id", None)
        task_id = ""
        if state is not None and l2_phase:
            task_id = self._begin_l2_agent_ask(
                state,
                agent,
                agent_name=agent_name,
                phase=l2_phase,
                attempt=l2_attempt,
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
                self._finish_l2_agent_ask(
                    state,
                    agent,
                    agent_name=agent_name,
                    phase=l2_phase,
                    attempt=l2_attempt,
                    task_id=task_id,
                    elapsed_ms=elapsed_ms,
                    stop_reason="error",
                )
            return "", {"total_ms": elapsed_ms, "internal": {}}
        elapsed_ms = int((time.time() - t0) * 1000)
        internal = getattr(agent, "_last_run_node_timings", None) or {}
        last_ts = getattr(agent, "_last_task_state", None)
        if state is not None and task_id:
            self._finish_l2_agent_ask(
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
        return answer, {"total_ms": elapsed_ms, "internal": dict(internal)}

    def _begin_repair_trace(self, state: RepairState) -> None:
        import uuid

        from agent_runtime.run_coordination import RunCoordinator

        if not state.repair_run_id:
            state.repair_run_id = "repair-" + uuid.uuid4().hex
        coordinator = RunCoordinator(
            self._repo_root,
            state.repair_run_id,
            state.repair_run_id,
            state_root=str(
                getattr(getattr(self.patcher, "tool_context", None), "state_root", "") or ""
            ),
        )
        coordinator.acquire()
        self._entry_coordinator = coordinator
        heartbeat_stop = threading.Event()

        def maintain_entry_owner():
            while not heartbeat_stop.wait(coordinator.lease_seconds / 3):
                try:
                    coordinator.heartbeat()
                except Exception:
                    return  # Preserve the durable fence on failure or replacement.

        heartbeat_worker = threading.Thread(
            target=maintain_entry_owner,
            name="fixloop-entry-owner",
            daemon=True,
        )
        heartbeat_worker.start()
        try:
            self._initialize_repair_trace(state)
        except BaseException:
            current = coordinator.store.snapshot(state.repair_run_id)
            if current.owner_token == coordinator.lease.owner_token:
                current = coordinator.finish(
                    "recovery_required", error_code="repair_initialization_interrupted"
                )
                state.control.coordination_status = current.status
            raise
        finally:
            # RepairPlanBinding starts its normal heartbeat before handoff.
            heartbeat_stop.set()
            heartbeat_worker.join(timeout=1)

    def _telemetry(self):
        from src.repair.telemetry_service import RepairTelemetryService

        return RepairTelemetryService(
            self._repo_root,
            self._repair_ctx,
            self.patcher,
            self._active_agents,
            self.l1_prompt_cache_key,
            self._on_harness_event,
        )

    def _initialize_repair_trace(self, state):
        self._telemetry().initialize(state)
        run_id = state.repair_run_id
        try:
            from src.collaboration.repair_runtime import RepairCollaborationRuntime

            self._collaboration_runtime = RepairCollaborationRuntime(
                self._repo_root,
                run_id,
                state,
                state_root=str(
                    getattr(getattr(self.patcher, "tool_context", None), "state_root", "") or ""
                ),
            )
        except Exception as exc:
            self._collaboration_runtime = None
            state.agent_errors.setdefault("collaboration_runtime", str(exc)[:500])
        if self._repair_ctx is not None and self._repair_ctx.worktree_handle is not None:
            self._repair_ctx.worktree_initial_snapshot = self._snapshot_repo()
        from src.repair.plan_binding import RepairPlanBinding

        self._plan_binding = RepairPlanBinding(self, state, defer_plan=True)

    def _end_repair_trace(self, state: RepairState) -> None:
        ctx = self._repair_ctx
        binding = getattr(self, "_plan_binding", None)
        coordinator = getattr(binding, "coordinator", None)
        if coordinator is not None and getattr(coordinator, "lease", None) is not None:
            try:
                backend = getattr(self._sandbox_context, "sandbox_backend", None)
                if backend is not None and hasattr(backend, "close"):
                    self._sandbox_backend_close_attempted = True
                    backend.close()
                token = coordinator.cancel_token or (
                    getattr(ctx, "cancel_token", None) if ctx is not None else None
                )
                service = getattr(self, "_verify_service", None)
                if service is not None and not service.cleanup_confirmed:
                    self._block_unconfirmed_verification(state, "verification cleanup unconfirmed")
                    coordinator.finish(
                        "recovery_required", error_code="verification_cleanup_unconfirmed"
                    )
                elif state.control.coordination_status == "recovery_required":
                    coordinator.finish(
                        "recovery_required", error_code="rollback_outcome_unconfirmed"
                    )
                elif token is not None and token.is_cancelled:
                    self._cancel_run_resources(state, finalize=True)
                elif str(getattr(state, "status", "") or "").lower() in {"failed", "error"}:
                    snapshot = coordinator.finish("failed", error_code="repair_failed")
                    state.control.coordination_status = snapshot.status
                else:
                    snapshot = coordinator.finish("released")
                    state.control.coordination_status = snapshot.status
                if state.control.coordination_status == "recovery_required":
                    state.set_status("recovery_required", "cleanup_or_write_unconfirmed")
            except Exception as exc:
                state.agent_errors.setdefault("coordination_finalize", str(exc)[:500])
                state.control.coordination_status = "recovery_required"
                state.set_status("recovery_required", "coordination_finalize_unconfirmed")
            previous = state.recovery_outcome
            binding.publish_recovery(
                stage=previous.get("stage", "resources"),
                reason_code=previous.get("reason_code", ""),
            )
        if state.control.coordination_status != "recovery_required":
            self._maybe_leave_worktree(cancelled=False)
        elif ctx is not None and ctx.worktree_handle is not None:
            state.execution.worktree_recovery = ctx.worktree_handle.as_dict()
            state.node_timings["worktree_recovery"] = state.execution.model_dump()[
                "worktree_recovery"
            ]
        runtime = getattr(self, "_collaboration_runtime", None)
        if runtime is not None and hasattr(runtime, "close"):
            try:
                runtime.close()
            except Exception as exc:
                state.agent_errors.setdefault("collaboration_runtime_close", str(exc)[:500])
        self._telemetry().finalize(state)

    def _reset_token_tracking(self):
        self._telemetry()._reset_token_tracking()

    def _attach_token_usage(self, state):
        self._telemetry()._attach_token_usage(state)

    def _attach_rejection_stats(self, state):
        self._telemetry()._attach_rejection_stats(state)

    def _run_patcher(self, state: RepairState) -> tuple[list[CandidatePatch], dict]:
        """Run the sole patch path: governed tools mutate disk, Runtime snapshots the diff."""

        plan = state.repair_plan
        self._merge_blackboard_for_patch(state)
        feedback = state.feedback or ""
        # 失败面：注入断言 + 失败测试原文，驱动定点读改
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

        # Terminal status is durable for reporting, but phase/evidence markers
        # are scoped to one model attempt.  Start the retry in locating mode
        # while retaining terminal history and feedback below.
        begin_patcher_attempt(state)

        surface = build_fail_surface(
            state, repo_root=self._repo_root, sandbox_context=self._sandbox_context
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
                repo_root=self._repo_root,
                result=state.verification_result,
                sandbox_context=self._sandbox_context,
            )
            state.control.structured_verify_feedback = payload.to_dict()
            structured_feedback = payload.to_dict()
        if isinstance(structured_feedback, dict):
            feedback_block = render_verify_feedback_block(structured_feedback)
            if feedback_block and feedback_block not in feedback:
                feedback = f"{feedback_block}\n\n{feedback}".strip() if feedback else feedback_block

        allowed = list(state.control.allowed_edit or [])
        runtime_bits = [
            "【repair runtime】read → apply_patch"
            "（*** Begin/End Patch，Update 须含 - 上下文）→ quick_test。",
            "可 expand_lock；不要用 run_shell/sandbox_test（无权限）。真源=磁盘 diff。",
            "expand_lock operation=modify|create|delete；新增文件用 create 授权后 Add File，"
            "修改和删除须重新读取。",
        ]
        if allowed:
            runtime_bits.append("allowed_edit: " + ", ".join(allowed[:12]))
        block = "\n".join(runtime_bits)
        feedback = f"{block}\n\n{feedback}".strip() if feedback else block

        suspects_for_prompt = state.suspect_locations
        patch_steps = self.repair_config.patcher_max_steps
        if self.repair_config.snapshot()["provenance"]["patcher_max_steps"] == "default":
            patch_steps = self.patcher.config.max_steps
        depth_token = push_patcher_depth(self.patcher, patch_steps)
        state.node_timings["patch_steps"] = patch_steps

        prompt, tpl_meta = self._patcher_prompt(
            suspects_for_prompt,
            state.retrieved_context,
            feedback,
            plan=plan,
            issue=state.issue_input,
            runtime_contract_block=render_patcher_runtime_contract(state),
            allowed_edit=allowed,
        )

        try:
            applied, meta = self._run_patcher_toolized(state, prompt, tpl_meta)
            status = classify_patcher_attempt(
                state,
                applied,
                apply_failed=bool(state.agent_errors.get("patcher_apply")),
                terminal_answer=str(meta.get("agent_answer") or ""),
                agent_stop_reason=str(meta.get("agent_stop_reason") or ""),
            )
            record_patcher_terminal_status(state, status, reason="patcher_turn_complete", meta=meta)
            return applied, meta
        finally:
            pop_patcher_depth(self.patcher, depth_token)

    def _compact_patcher_history(self, state) -> None:
        """CC 序 compact；thrash 时记停损标志。"""
        if self.patcher is None:
            return
        from src.repair.context_compact import compact_tool_history

        session = getattr(self.patcher, "session", None) or {}
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

    def _run_patcher_toolized(
        self,
        state: RepairState,
        prompt: str,
        tpl_meta: dict,
    ) -> tuple[list[CandidatePatch], dict]:
        from agent_runtime.cancellation import CancelledError
        from agent_runtime.run_coordination import CoordinationError
        from src.repair.plan_binding import RepairPlanBinding

        try:
            if self._plan_binding is None:
                self._plan_binding = RepairPlanBinding(self, state)
            return self._plan_binding.run_patcher(
                prompt,
                tpl_meta,
                lambda p, m: self._run_patcher_agent(state, p, m),
            )
        except CancelledError:
            return [], {"user_cancel": True, "edit_mode": "cancelled", "total_ms": 0}
        except (ValueError, OSError, CoordinationError) as exc:
            if self._is_repair_cancelled():
                return [], {"user_cancel": True, "edit_mode": "cancelled", "total_ms": 0}
            state.agent_errors["plan_runtime"] = str(exc)
            state.control.plan_blocked = True
            return [], {
                "total_ms": 0,
                "model_call_ms": 0,
                "parse_apply_ms": 0,
                "plan_blocked": str(exc),
                "edit_mode": "plan_blocked",
            }

    def _run_patcher_agent(
        self,
        state: RepairState,
        prompt: str,
        tpl_meta: dict,
    ) -> tuple[list[CandidatePatch], dict]:
        """Agent loop：工具改盘 → 快照 diff 成 CandidatePatch（不再二次 apply）。"""
        from src.repair.execution.edit_from_disk import patches_from_snapshot_diff
        from src.repair.failure_tags import check_patch_faithfulness

        t_start = time.time()
        compact_enabled = self.repair_config.patcher_compact
        if compact_enabled:
            self._compact_patcher_history(state)
        before = self._snapshot_repo()
        observation_start = len(
            (getattr(self.patcher, "session", {}) or {}).get("tool_observations", [])
        )
        answer, agent_timing = self._run_agent(
            self.patcher,
            prompt,
            "patcher",
            state,
            l2_phase="patch",
            l2_attempt=state.retry_count,
        )
        if self._plan_binding is not None and self._plan_binding.exploration is not None:
            self._plan_binding.exploration.drain(cancel=True)
        context = self.patcher.tool_context
        if context.execution_uncertain or context.sandbox_uncertain:
            state.control.coordination_status = "recovery_required"
            state.set_status("recovery_required", "patcher_write_unconfirmed")
            total_ms = int((time.time() - t_start) * 1000)
            return [], {
                "total_ms": total_ms,
                "model_call_ms": int(agent_timing.get("total_ms") or total_ms),
                "parse_apply_ms": 0,
                "edit_mode": "recovery_required",
                "execution_uncertain": True,
            }
        after = self._snapshot_repo()
        observations = list(
            (getattr(self.patcher, "session", {}) or {}).get("tool_observations", [])
        )[observation_start:]
        write_tools = {"write_file", "patch_file", "apply_patch"}
        write_attempted = any(
            str(item.get("tool_name") or "") in write_tools for item in observations
        )
        state.control.patcher_write_attempted = bool(write_attempted)
        lock = getattr(self, "_edit_lock", None)
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
            if hasattr(self, "_progress_emitter"):
                summary = (
                    f"apply_ok={lock.apply_patch_ok_count} lint_rej={lock.edit_lint_reject_count}"
                )
                self._progress_emitter().emit("tool_progress", summary=summary)
                if hasattr(self, "_emit_repair_span"):
                    self._emit_repair_span(
                        "apply_patch_span",
                        {
                            "summary": summary,
                            "apply_patch_ok_count": lock.apply_patch_ok_count,
                        },
                    )
        patches = patches_from_snapshot_diff(before, after)
        if write_attempted and not patches:
            if lock is not None and (
                getattr(lock, "unread_write_reject_count", 0)
                or getattr(lock, "apply_path_reject_count", 0)
                or getattr(lock, "edit_lint_reject_count", 0)
            ):
                state.control.patcher_write_rejected = True
            else:
                state.control.patcher_export_failed = True
        patches, rejected = check_patch_faithfulness(
            patches, state, soft_keep=False, repo_root=str(self._repo_root or "")
        )
        if rejected:
            state.agent_errors["patcher_hallucinated_file"] = rejected[0]
            for rel in rejected:
                if rel in before:
                    target = Path(self._repo_root) / rel
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(before[rel], encoding="utf-8")
                    except OSError as e:
                        log.warning("[patcher] restore rejected file %s: %s", rel, e)
            # 重新 diff 仅保留 allowed（含 soft 晋升）
            after = self._snapshot_repo()
            patches = patches_from_snapshot_diff(before, after)
            patches, _ = check_patch_faithfulness(
                patches, state, soft_keep=False, repo_root=str(self._repo_root or "")
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
                getattr(getattr(self.patcher, "_last_task_state", None), "stop_reason", "") or ""
            ),
        }
        if not patches:
            state.node_timings["patcher_edit_mode"] = "tools_empty"
            return [], meta
        state.node_timings["patcher_edit_mode"] = "tools"
        self._last_apply_errors = []
        self._last_sibling_warnings = []
        log.info("[patcher] toolized 落盘 %d 个文件", len(patches))
        return patches, meta

    def _pick_test_path(self, state: RepairState) -> str:
        """Prefer actual failure targets, then public repository test references."""
        from src.repair.verification.fail_surface import preferred_verify_targets
        from src.repair.verification.test_references import normalize_related_test_refs

        candidates = list(preferred_verify_targets(state))
        context = state.retrieved_context
        if context is not None:
            for item in context.related_tests or []:
                if isinstance(item, str):
                    candidates.append(item)
                elif isinstance(item, dict):
                    for key in ("nodeid", "name", "path"):
                        if value := item.get(key):
                            candidates.append(str(value))
                            break
        if getattr(self, "_sandbox_context", None) is not None:
            from agent_runtime.linux_sandbox.command_profile import test_target

            targets = []
            for candidate in candidates:
                try:
                    targets.append(test_target(candidate))
                except ValueError:
                    continue
        else:
            targets = normalize_related_test_refs(candidates, self._repo_root or None)
        return targets[0] if targets else ""

    def _build_feedback(self, result, *, state=None):
        from src.repair.feedback_service import RepairFeedbackService

        ctx = self._repair_ctx
        tracer = ctx.repair_tracer if ctx else None
        return RepairFeedbackService(
            self._repo_root, self._sandbox_context, tracer.emit if tracer else None
        ).build(result, state=state)

    # ---- Prompt 构建 ----

    def _emit_skill_hint_trace(self, agent: str, render) -> None:
        from src.skills.skill_block import skill_hint_rendered_trace

        tracer = self._repair_ctx.repair_tracer if self._repair_ctx else None
        if tracer is None or not render.text:
            return
        tracer.emit(agent, "skill_hint_rendered", skill_hint_rendered_trace(render))

    def _patcher_prompt(
        self,
        suspects: list[SuspectLocation],
        context: RetrievedContext | None,
        feedback: str = "",
        plan: RepairPlan | None = None,
        issue: str = "",
        runtime_contract_block: str = "",
        allowed_edit: list[str] | None = None,
    ) -> tuple[str, dict]:
        ctx = self._repair_ctx
        blackboard = ctx.blackboard if ctx is not None else None
        evidence_block = ""
        if self.patcher is not None:
            try:
                from agent_runtime.features.memory import render_evidence_ledger

                memory = (
                    self.patcher.session.get("memory", {})
                    if getattr(self.patcher, "session", None)
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
            read_snippet=self._read_code_snippet,
            read_test_context=self._read_test_context,
            fallback_suspects=self._fallback_suspects_from_plan,
            blackboard=blackboard,
            diff_only=True,
            read_line_range=self._read_line_range,
            runtime_contract_block=runtime_contract_block,
            allowed_edit=allowed_edit,
        )
        tracer = ctx.repair_tracer if ctx is not None else None
        if subscribe_meta and tracer is not None:
            tracer.emit(
                "orchestrator",
                "blackboard_prefix_subscribed",
                subscribe_meta,
            )
        self._emit_skill_hint_trace("patcher", render)
        return render_repair_task("patcher", variables)

    def _context_reader(self):
        from src.repair.context_reader import RepairContextReader

        return RepairContextReader(self._repo_root)

    def _read_code_snippet(self, file_path, start_line, end_line):
        return self._context_reader()._read_code_snippet(file_path, start_line, end_line)

    def _read_line_range(self, file_path, start_line, end_line):
        return self._context_reader()._read_line_range(file_path, start_line, end_line)

    def _read_test_context(self, context, suspects, plan=None):
        return self._context_reader()._read_test_context(context, suspects, plan)

    @staticmethod
    def _select_test_excerpt(content, target_names, *, max_chars, max_lines):
        from src.repair.context_reader import RepairContextReader

        return RepairContextReader._select_test_excerpt(
            content, target_names, max_chars=max_chars, max_lines=max_lines
        )

    def _resolve_test_path(self, ref):
        return self._context_reader()._resolve_test_path(ref)

    def _guess_test_file(self, source_file, function_name):
        return self._context_reader()._guess_test_file(source_file, function_name)

    def _discover_repo_test_files(self):
        return self._context_reader()._discover_repo_test_files()

    def _parse_verification(self, answer: str) -> "VerificationResult":
        return parse_verification(answer)
