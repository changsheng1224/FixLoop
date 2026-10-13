"""Repair workspace, cancellation and execution resource lifecycle."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import contextmanager
from pathlib import Path

from agent_runtime.logging_setup import get_logger
from src.repair.phase_clock import PhaseTimeoutConfig
from src.repair.run_context import RepairRunContext
from src.state import RepairState

log = get_logger("repair.lifecycle")
DEFAULT_REPAIR_TIMEOUT_S = 180


class ExecutionLifecycleMixin:
    def repair(
        self,
        issue: str,
        max_retries: int = 3,
        repair_timeout_s: int = DEFAULT_REPAIR_TIMEOUT_S,
        phase_timeouts: PhaseTimeoutConfig | None = None,
        cancel_token=None,
        resume_run_id: str = "",
        *,
        run_id: str = "",
        workspace_ready=None,
    ) -> RepairState:
        """执行修复流水线。

        Args:
            issue: Issue 描述（含堆栈和错误信息）。
            max_retries: 最大重试次数。
            repair_timeout_s: 全流程超时秒数（≤0 表示不限制）。
            phase_timeouts: 分阶段超时；默认由 ``repair_timeout_s`` 推导。
            cancel_token: 可选协作式取消 token（CLI Ctrl+C 注入）。
            resume_run_id: L2 续跑 run_id（从 repair_checkpoint.json 恢复，
                跳过 parse/localize，直接进入 patch 循环）。
            run_id: 新任务的可选 ID；不能与严格恢复 resume_run_id 同时使用。

        Returns:
            RepairState 实例。
        """
        from agent_runtime.cancellation import CancellationToken

        if run_id and resume_run_id:
            raise ValueError("run_id and resume_run_id are mutually exclusive")
        if (
            self._sandbox_task_spec is None
            and self._sandbox_context is not None
            and not getattr(getattr(self.patcher, "tool_context", None), "state_root", "")
        ):
            raise ValueError("wsl_bwrap repair requires P3 external state_root isolation")

        if phase_timeouts is None:
            phase_timeouts = PhaseTimeoutConfig.for_patcher_primary(repair_timeout_s)

        state = RepairState(
            issue_input=issue,
            max_retries=max_retries,
            repair_run_id=resume_run_id or run_id or "",
            active_roles=[
                role
                for role, agent in (("patcher", self.patcher), ("verifier", self.verifier))
                if agent is not None
            ],
            field_owners={
                "phase": "runtime",
                "status": "runtime",
                "changed_files": "patch_runtime",
                "verification_result": "verifier",
                "tool_budget": "runtime",
            },
        )
        state.intent = {"mode": "repair", "verification_required": True}
        state.role_lifecycle = {
            role: {"status": "active", "reason": "initial repair roles"}
            for role in state.active_roles
        }
        previous = getattr(self, "_verify_service", None)
        if previous is not None and (previous.active.is_set() or not previous.cleanup_confirmed):
            self._block_unconfirmed_verification(state, "previous verification cleanup unconfirmed")
            return state
        self._verify_service = None
        sandbox_prepared = False
        sandbox_pipeline_started = False
        if self._sandbox_task_spec is not None:
            import uuid

            state.repair_run_id = state.repair_run_id or "repair-" + uuid.uuid4().hex
            self._prepare_sandbox_task(state, resume=bool(resume_run_id))
            sandbox_prepared = True
        from src.repair.checkpoint_load import RepairCheckpointError, load_repair_checkpoint
        from src.repair.recovery_outcome import publish_recovery_outcome

        try:
            if self._sandbox_task_spec is None and self._workspace_policy == "worktree":
                self._prepare_host_worktree(state, resume=bool(resume_run_id))
            entry = self._workspace_entry()
            state.node_timings["run_entry"] = entry
            checkpoint = None
            if workspace_ready is not None:
                workspace_ready(self._repo_root)
            if resume_run_id:
                try:
                    checkpoint = load_repair_checkpoint(
                        self._repo_root,
                        resume_run_id,
                        state_root=str(
                            getattr(getattr(self.patcher, "tool_context", None), "state_root", "")
                            or ""
                        ),
                        require_valid=True,
                        issue=issue,
                    )
                    binding = getattr(self._sandbox_context, "sandbox_task_binding", None)
                    if binding is not None and (
                        checkpoint.get("node_timings", {}).get("sandbox_task_binding")
                        != binding.to_wire()
                    ):
                        raise RepairCheckpointError("sandbox_task_identity_mismatch")
                    if checkpoint.get("node_timings", {}).get("run_entry") != entry:
                        raise RepairCheckpointError("resume_workspace_policy_mismatch")
                except RepairCheckpointError as exc:
                    state.set_status("recovery_required", exc.code)
                    state.agent_errors["checkpoint"] = exc.code
                    publish_recovery_outcome(
                        state,
                        {"run_id": resume_run_id, "status": "recovery_required"},
                        stage="checkpoint",
                        reason_code=exc.code,
                        emitter=self._progress_emitter(),
                    )
                    return state
            token = cancel_token or CancellationToken()
            initial_snapshot = self._snapshot_repo()

            self._repair_ctx = RepairRunContext(
                phase_timeout_config=phase_timeouts,
                cancel_token=token,
                resume_checkpoint=checkpoint,
                worktree_handle=self._sandbox_worktree_handle,
            )
            self._initialize_model_budget(checkpoint, repair_timeout_s)
            self._set_collaboration_context(state)
            if token.is_cancelled and not resume_run_id:
                state.control.user_cancel = True
                state.set_status("user_cancel", "cancelled_before_execution")
                publish_recovery_outcome(
                    state,
                    {
                        "run_id": state.repair_run_id,
                        "status": "cancelled",
                        "cancel_request_id": "before_execution",
                    },
                    stage="cancel",
                    reason_code="cancelled_before_execution",
                    emitter=self._progress_emitter(),
                )
                return state
            with self._repair_cancel_scope(token):
                if repair_timeout_s <= 0:
                    sandbox_pipeline_started = True
                    return self._repair_impl(state, initial_snapshot=initial_snapshot)

                # Phase C：勿用 with ThreadPoolExecutor（退出时 wait=True 会拖尾）
                pool = ThreadPoolExecutor(max_workers=1)
                wall_started = time.monotonic()
                fut = pool.submit(self._repair_impl, state, initial_snapshot)
                sandbox_pipeline_started = True
                try:
                    return fut.result(timeout=repair_timeout_s)
                except FuturesTimeoutError:
                    from src.repair.verification.repair_timeout import handle_repair_wall_timeout

                    handle_repair_wall_timeout(
                        self,
                        state,
                        initial_snapshot=initial_snapshot,
                        repair_timeout_s=repair_timeout_s,
                        cancel_token=token,
                        wall_started=wall_started,
                    )
                    return state
                finally:
                    pool.shutdown(wait=False, cancel_futures=True)
        finally:
            self._release_model_budget(state)
            self._repair_ctx = None
            if sandbox_prepared and not sandbox_pipeline_started:
                self._cleanup_sandbox_startup_failure(state)
            elif (
                self._sandbox_task_spec is None
                and self._sandbox_worktree_handle is not None
                and not sandbox_pipeline_started
            ):
                from agent_runtime.worktree import WorktreeError, retain_worktree

                try:
                    retain_worktree(self._sandbox_worktree_handle, reason="startup_stopped")
                except (WorktreeError, OSError) as exc:
                    state.agent_errors.setdefault("worktree_retention", str(exc)[:500])

    def _initialize_model_budget(self, checkpoint, timeout_s):
        from agent_runtime.budget_manager import BudgetManager
        from agent_runtime.repair_runtime import ExecutionDeadline

        config = getattr(self.patcher, "config", None)
        if config is None or not hasattr(config, "effective_budget"):
            return
        self._model_budget = BudgetManager.from_config(config)
        saved = (checkpoint or {}).get("node_timings", {})
        if saved.get("model_budget"):
            self._model_budget.restore(saved["model_budget"])
        limits = [s for s in (timeout_s, config.deadline.repair_s) if s > 0]
        self._model_deadline = ExecutionDeadline(min(limits) if limits else 0)
        if saved.get("model_deadline"):
            remaining = saved["model_deadline"].get("remaining_s")
            current = self._model_deadline.remaining_s()
            if remaining is not None:
                self._model_deadline = ExecutionDeadline.from_remaining(
                    min(remaining, current) if current is not None else remaining
                )
        for agent in self._active_agents:
            agent._model_budget_run_bound = True
            agent._standalone_model_budget = False
            agent._run_budget_manager = self._model_budget
            agent._repair_deadline = self._model_deadline

    def _release_model_budget(self, state):
        budget = getattr(self, "_model_budget", None)
        if budget is None:
            return
        state.node_timings["model_budget"] = budget.snapshot()
        state.node_timings["model_deadline"] = self._model_deadline.snapshot()
        for agent in self._active_agents:
            if getattr(agent, "_run_budget_manager", None) is budget:
                agent._run_budget_manager = None
                agent._repair_deadline = None
                agent._model_budget_run_bound = False
        self._model_budget = None
        self._model_deadline = None

    def _workspace_entry(self) -> dict:
        """Trusted entry decisions; checkpoint recovery must reproduce them exactly."""

        def directory(root):
            path = Path(root).resolve(strict=True)
            stat = path.stat()
            return {"root": str(path), "identity": [stat.st_dev, stat.st_ino]}

        return {
            "workspace_policy": self._workspace_policy,
            "execution_profile": self._execution_profile,
            "sandbox_policy": self.sandbox_policy,
            "project": directory(self._project_root or self._sandbox_repository_root),
            "workspace": directory(self._repo_root),
        }

    def _prepare_host_worktree(self, state: RepairState, *, resume: bool) -> None:
        """Pin the final host checkout before snapshots, checkpoints and coordination."""
        import uuid

        from agent_runtime.state_root import managed_state_base, trusted_state_root
        from agent_runtime.worktree import adopt_worktree, create_worktree, default_worktree_path

        repository = self._sandbox_repository_root
        if self._project_root and Path(self._project_root).resolve() != Path(repository).resolve():
            raise ValueError("worktree source must be the registered project root")
        if self._sandbox_context is not None:
            raise ValueError("prebound sandbox cannot change workspace; use sandbox_task_spec")
        if self._sandbox_worktree_handle is not None:
            raise ValueError("workspace_already_bound: use a fresh orchestrator")
        state.repair_run_id = state.repair_run_id or "repair-" + uuid.uuid4().hex
        base = self._workspace_state_base or None
        if resume:
            handle = adopt_worktree(
                repository,
                default_worktree_path(repository, state.repair_run_id, state_base=base),
                state.repair_run_id,
                state_base=base,
                ownership="task",
                branch=f"fixloop/wt-{state.repair_run_id}",
            )
        else:
            handle = create_worktree(repository, state.repair_run_id, state_base=base)
        self._sandbox_worktree_handle = handle
        self._repo_root = str(handle.path)
        state_root = str(
            trusted_state_root(
                handle.path, managed_state_base(repository, base) / "tasks" / state.repair_run_id
            )
        )
        for agent in self._active_agents:
            binder = getattr(agent, "bind_repair_workspace", None)
            if callable(binder):
                binder(self._repo_root, state_root, repository, state.repair_run_id)
            else:
                tc = getattr(agent, "tool_context", None)
                if tc is not None:
                    tc.root, tc.state_root = self._repo_root, state_root
                agent._cwd, agent.state_root = self._repo_root, state_root
        self.l1_prompt_cache_key = self._resolve_l1_prompt_cache_key()

    @contextmanager
    def _repair_cancel_scope(self, token):
        """绑定 cancel token 到子 Agent，退出时解绑。"""
        ctx = self._repair_ctx
        if ctx is not None:
            ctx.cancel_token = token
        self._bind_cancel_token(token)
        try:
            yield
        finally:
            self._unbind_cancel_token()

    def _abort_repair_if_cancelled(self, state: RepairState) -> bool:
        """若用户已 cancel 则返回 True（由 pipeline finally 负责 restore）。"""
        return (
            self._is_repair_cancelled() or state.control.coordination_status == "recovery_required"
        )

    def _bind_cancel_token(self, token) -> None:
        for agent in self._active_agents:
            agent.cancel_token = token

    def _unbind_cancel_token(self) -> None:
        for agent in self._active_agents:
            agent.cancel_token = None

    def _is_repair_cancelled(self) -> bool:
        ctx = self._repair_ctx
        token = ctx.cancel_token if ctx is not None else None
        return token is not None and token.is_cancelled

    def _emit_repair_cancelled(self, state: RepairState) -> None:
        ctx = self._repair_ctx
        confirmed = state.control.coordination_status != "recovery_required"
        if confirmed:
            self._maybe_leave_worktree(cancelled=True)
        harness = getattr(ctx, "harness_control", None) if ctx is not None else None
        if harness is not None and confirmed:
            harness.request_control("cancel", reason="repair cancelled")
            harness.finish("cancelled", reason="repair cancelled")
        tracer = ctx.repair_tracer if ctx is not None else None
        if tracer is None:
            return
        tracer.close_dangling_ask_spans()
        tracer.emit(
            "orchestrator",
            "repair_cancelled",
            {
                "status": state.status,
                "repo_restored": confirmed,
            },
            status="cancelled" if confirmed else "recovery_required",
        )
        tracer.end_root_span()

    def _prepare_sandbox_task(self, state: RepairState, *, resume: bool) -> None:
        """Resolve the actual checkout before creating any backend or runtime state."""
        from agent_runtime.worktree import WorktreeError, retain_worktree

        if getattr(self._sandbox_context, "sandbox_task_binding", None) is not None:
            raise ValueError("sandbox_task_already_bound: use a fresh task context")
        try:
            self._bind_sandbox_task(state, resume=resume)
        except BaseException as exc:
            if not resume and self._sandbox_worktree_handle is not None:
                try:
                    retain_worktree(self._sandbox_worktree_handle, reason="startup_failed")
                except (WorktreeError, OSError) as retention_error:
                    exc.add_note(f"worktree retention metadata unavailable: {retention_error}")
            raise

    def _cleanup_sandbox_startup_failure(self, state: RepairState) -> None:
        """Release a task bound before the repair pipeline acquired its owner."""
        from agent_runtime.worktree import WorktreeError, retain_worktree

        try:
            self._close_sandbox_backend_before_binding_close(state)
        except Exception as exc:
            # Startup has no plan coordinator yet. Keep the original failure and
            # surface cleanup uncertainty on the returned state where possible.
            state.agent_errors.setdefault("sandbox_cleanup", str(exc)[:500])
            state.set_status("recovery_required", "sandbox_cleanup_unconfirmed")
        handle = self._sandbox_worktree_handle
        if handle is None:
            return
        try:
            retain_worktree(handle, reason="startup_failed")
        except (WorktreeError, OSError) as exc:
            state.agent_errors.setdefault("worktree_retention", str(exc)[:500])

    def _bind_sandbox_task(self, state: RepairState, *, resume: bool) -> None:
        from agent_runtime.linux_sandbox.task_context import SandboxTaskBinding
        from agent_runtime.worktree import adopt_worktree, create_worktree

        spec = self._sandbox_task_spec
        repository = self._sandbox_repository_root
        if resume:
            binding = SandboxTaskBinding.load(repository, state.repair_run_id, spec)
            expected = "task" if self._workspace_policy == "worktree" else "user"
            if binding.workspace_ownership != expected:
                raise ValueError("resume_workspace_policy_mismatch")
            if binding.workspace_ownership == "task":
                self._sandbox_worktree_handle = adopt_worktree(
                    repository,
                    binding.mapping.host_root,
                    binding.task_id,
                    state_base=spec.state_base or None,
                    ownership="task",
                    branch=binding.worktree_branch,
                )
        else:
            workspace = repository
            ownership, branch = "user", ""
            if self._workspace_policy == "worktree":
                handle = create_worktree(
                    repository, state.repair_run_id, state_base=spec.state_base or None
                )
                self._sandbox_worktree_handle = handle
                workspace, ownership, branch = handle.path, "task", handle.branch
            binding = SandboxTaskBinding.create(
                repository, workspace, state.repair_run_id, spec, ownership=ownership, branch=branch
            )
            binding.save()
        # The factory receives the immutable final mapping, never the source repo.
        backend = self._sandbox_backend_factory(binding)
        if backend is None:
            raise ValueError("sandbox_backend_binding_mismatch")
        if getattr(backend, "task_binding", None) != binding:
            try:
                backend.close()
            except Exception:
                pass
            raise ValueError("sandbox_backend_binding_mismatch")
        self._sandbox_backend_close_attempted = False
        self._repo_root = binding.mapping.host_root
        try:
            for agent in self._active_agents:
                binder = getattr(agent, "bind_sandbox_task", None)
                if callable(binder):
                    binder(binding, backend)
                    continue
                tc = getattr(agent, "tool_context", None)
                if tc is not None:
                    tc.root = self._repo_root
                    binding.attach(tc)
                    tc.sandbox_backend = backend
                if hasattr(agent, "_cwd"):
                    agent._cwd = self._repo_root
                if hasattr(agent, "state_root"):
                    agent.state_root = binding.state_root
        except BaseException as exc:
            # Backend construction pins the environment before Agent binding.
            # If binding fails, close that pin before propagating the startup
            # error; no coordinator exists yet to perform this cleanup later.
            try:
                backend.close()
            except Exception as cleanup_error:
                exc.add_note(f"sandbox startup cleanup unconfirmed: {cleanup_error}")
            raise
        state.node_timings["sandbox_task_binding"] = binding.to_wire()
        state.node_timings["run_entry"] = self._workspace_entry()

    def _close_sandbox_backend_before_binding_close(self, state: RepairState) -> None:
        """Close sandbox admission before an exceptional plan-binding teardown."""
        context = getattr(self, "_sandbox_context", None)
        backend = getattr(context, "sandbox_backend", None)
        if (
            backend is None
            or getattr(backend, "_closed", False)
            or self._sandbox_backend_close_attempted
        ):
            return
        self._sandbox_backend_close_attempted = True
        try:
            backend.close()
        except Exception as exc:
            state.agent_errors.setdefault("sandbox_cleanup", str(exc)[:500])
            state.set_status("recovery_required", "sandbox_cleanup_unconfirmed")
            binding = getattr(self, "_plan_binding", None)
            coordinator = getattr(binding, "coordinator", None)
            lease = getattr(coordinator, "lease", None)
            if coordinator is not None and lease is not None:
                try:
                    snapshot = coordinator.finish(
                        "recovery_required", error_code="sandbox_cleanup_unconfirmed"
                    )
                    state.control.coordination_status = snapshot.status
                except Exception as coordination_error:
                    state.agent_errors.setdefault(
                        "coordination_finalize", str(coordination_error)[:500]
                    )

    def _maybe_leave_worktree(self, *, cancelled: bool = False) -> None:
        """保留任务 checkout；固定绑定的工作区不随任务结束而切换。"""
        from agent_runtime.worktree import retain_worktree

        ctx = self._repair_ctx
        if ctx is None:
            return
        handle = getattr(ctx, "worktree_handle", None)
        if handle is not None:
            retain_worktree(handle, reason="cancelled" if cancelled else "task_ended")
            if ctx.repair_tracer is not None:
                ctx.repair_tracer.emit(
                    "orchestrator",
                    "worktree_retained",
                    {**handle.as_dict(), "cancelled": cancelled},
                    status="ok",
                )

    def _cancel_run_resources(
        self, state: RepairState, *, phase_returned: bool = True, finalize: bool = False
    ) -> bool:
        service = getattr(self, "_verify_service", None)
        if service is not None and (service.active.is_set() or not service.cleanup_confirmed):
            self._block_unconfirmed_verification(state, "verification process tree not stopped")
            return False
        binding = getattr(self, "_plan_binding", None)
        if binding is None:
            coordinator = getattr(self, "_entry_coordinator", None)
            if not phase_returned and coordinator is not None and coordinator.lease is not None:
                coordinator.store.mark_recovery_required(
                    coordinator.lease, error_code="repair_worker_unconfirmed"
                )
                state.control.coordination_status = "recovery_required"
                state.set_status("recovery_required", "repair_worker_unconfirmed")
                from src.repair.recovery_outcome import publish_recovery_outcome

                publish_recovery_outcome(
                    state,
                    coordinator.store.snapshot(state.repair_run_id).to_dict(),
                    stage="cancel",
                    reason_code="repair_worker_unconfirmed",
                    emitter=self._progress_emitter(),
                )
                return False
            return state.control.coordination_status != "recovery_required"
        runtime = getattr(self, "_collaboration_runtime", None)
        if runtime is not None and phase_returned:
            try:
                runtime.finish_cancelled(state)
            except Exception as exc:
                state.agent_errors["collaboration_cleanup"] = str(exc)[:500]
        report = binding.coordinator.cancel(finalize=finalize)
        state.node_timings["coordination_cancel"] = report.to_dict()
        state.control.coordination_status = report.status
        if not report.confirmed:
            state.set_status("recovery_required", "cleanup_or_write_unconfirmed")
        binding.publish_recovery(stage="cancel", reason_code=report.error_code)
        return report.confirmed
