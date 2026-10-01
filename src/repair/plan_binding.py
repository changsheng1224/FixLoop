"""One PlanSession across Patcher asks, orchestration retries and final verification."""

from __future__ import annotations

import copy
import json
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from agent_runtime.cancellation import CancellationToken, CancelledError, run_blocking
from agent_runtime.plan_runtime.models import digest
from agent_runtime.plan_runtime.planner import grounded_plan, retry_plan
from agent_runtime.plan_runtime.recovery import recover
from agent_runtime.plan_runtime.reducer import transition
from agent_runtime.plan_runtime.replan import check_replan
from agent_runtime.plan_runtime.scheduler import PlanScheduler, ReadBudget
from agent_runtime.plan_runtime.session import PlanSession
from agent_runtime.plan_runtime.workspace import snapshot
from agent_runtime.run_coordination import CoordinationError, RunCoordinator
from agent_runtime.tool_executor import QuotaEnforcer
from src.repair.replan_decision import decide_replan
from src.repair.stop_loss import has_stop_loss
from src.repair.verification.verify_diagnose import collect_log_excerpt
from src.tools.composite import build_repair_canonical_tools


class ResumeRecoveryRequiredError(CoordinationError):
    """Resume is fenced until all old writes and resources are confirmed."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("resume_recovery_required", reason)


class _ReadCancellationToken(CancellationToken):
    """Independent child cancellation with live propagation from the owner."""

    def __init__(self, parent):
        super().__init__()
        self.parent = parent

    @property
    def is_cancelled(self):
        return super().is_cancelled or bool(self.parent and self.parent.is_cancelled)

    @property
    def reason(self):
        if super().is_cancelled:
            return super().reason
        return self.parent.reason if self.parent else ""

    @property
    def cause(self):
        if super().is_cancelled:
            return super().cause
        return self.parent.cause if self.parent else None


class RepairPlanBinding:
    def __init__(self, orchestrator, state, *, defer_plan=False):
        self.orchestrator = orchestrator
        self.state = state
        if not state.repair_run_id:
            state.repair_run_id = "repair-" + uuid.uuid4().hex
        context = orchestrator.patcher.tool_context
        entry = getattr(orchestrator, "_entry_coordinator", None)
        self.coordinator = (
            entry
            if entry is not None and entry.run_id == state.repair_run_id
            else RunCoordinator(
                orchestrator._repo_root,
                state.repair_run_id,
                state.repair_run_id,
                state_root=str(getattr(context, "state_root", "") or ""),
            )
        )
        self.owner_lease = self.coordinator.lease or self.coordinator.acquire()
        self._closed = False
        self.exploration = None
        self.agent = orchestrator.patcher
        self.session = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat_worker = None
        self.coordinator.cancel_token = getattr(orchestrator.patcher, "cancel_token", None)
        self._cancel_unsubscribe = None
        try:
            # Entry ownership can predate slow Plan/Observation initialization.
            # Renew before initialization and keep reconciling owners alive too.
            self.owner_lease = self.coordinator.heartbeat()
            self._heartbeat_worker = threading.Thread(target=self._maintain_owner, daemon=True)
            self._heartbeat_worker.start()
            if self.coordinator.cancel_token is not None:
                self._cancel_unsubscribe = self.coordinator.cancel_token.add_callback(
                    self._request_cancel
                )
            context.run_coordinator = self.coordinator
            context.sandbox_owner_token = self.owner_lease.owner_token
            context.sandbox_generation = self.owner_lease.generation
            context.sandbox_coordination_revision = self.owner_lease.coordination_revision
            self._open(context, defer_plan=defer_plan)
        except BaseException:
            try:
                current = self.coordinator.store.snapshot(state.repair_run_id)
                if current.owner_token == self.coordinator.lease.owner_token:
                    if (
                        self.session is not None
                        and self.coordinator.cancel_token is not None
                        and self.coordinator.cancel_token.is_cancelled
                    ):
                        report = self.coordinator.cancel()
                        state.node_timings["coordination_cancel"] = report.to_dict()
                        state.node_timings["coordination_status"] = report.status
                    else:
                        current = self.coordinator.finish(
                            "recovery_required", error_code="binding_initialization_interrupted"
                        )
                        state.node_timings["coordination_status"] = current.status
                    self.publish_recovery(stage="resources")
            finally:
                self.close()
            raise

    def _maintain_owner(self):
        while not self._heartbeat_stop.wait(self.coordinator.lease_seconds / 3):
            try:
                self.coordinator.heartbeat()
            except Exception:
                return  # the durable fence rejects further dispatch

    def publish_recovery(self, *, stage, reason_code=""):
        from src.repair.recovery_outcome import publish_recovery_outcome

        current = self.coordinator.store.snapshot(self.state.repair_run_id)
        if current.generation != self.coordinator.lease.generation:
            return publish_recovery_outcome(
                self.state,
                {"run_id": self.state.repair_run_id, "status": "recovery_required"},
                stage="owner",
                reason_code="stale_generation",
                emitter=self.orchestrator._progress_emitter(),
            )
        return publish_recovery_outcome(
            self.state,
            current.to_dict(),
            stage=stage,
            plan_report=getattr(self, "report", None),
            reason_code=reason_code,
            emitter=self.orchestrator._progress_emitter(),
        )

    def _request_cancel(self):
        request_id = self.coordinator.request_cancel()
        self.publish_recovery(stage="cancel")
        return request_id

    def _open(self, context, *, defer_plan):
        orchestrator, state = self.orchestrator, self.state
        self.read_budget = ReadBudget(4)
        self.session = PlanSession(
            orchestrator._repo_root,
            state.repair_run_id,
            state.repair_run_id,
            self.agent.tools,
            state_root=context.state_root,
            observation_state=self.agent.session,
            event_sink=self._event,
            fault=getattr(orchestrator, "_plan_fault", None),
            owner_lease=self.owner_lease,
            coordination_store=self.coordinator.store,
        )
        from agent_runtime.run_coordination.adapters import (
            CollaborationTaskAdapter,
            CoordinationAdapters,
            PlanAttemptAdapter,
            SandboxCallAdapter,
        )

        collaboration = getattr(orchestrator, "_collaboration_runtime", None)
        from src.collaboration.exploration_runtime import ExplorationRuntime

        self.exploration = ExplorationRuntime(
            self.agent,
            run_id=state.repair_run_id,
            parent_task_id=state.repair_run_id,
            plan_session=self.session,
            coordinator=self.coordinator,
            client_factory=getattr(self.agent, "_exploration_client_factory", None),
            limits=getattr(self.agent, "_exploration_limits", None),
            event_sink=self._exploration_event,
        )
        context.readonly_exploration_runtime = self.exploration
        self.coordinator.adapters.update(
            CoordinationAdapters(
                PlanAttemptAdapter(self.session),
                CollaborationTaskAdapter(collaboration.store, state.repair_run_id)
                if collaboration is not None
                else None,
                SandboxCallAdapter(context.sandbox_backend)
                if context.sandbox_backend is not None
                else None,
            ).mapping()
        )
        coordination = self.coordinator.reconcile()
        self.publish_recovery(stage="resources")
        if coordination.get("status") != "active":
            state.node_timings["coordination_status"] = coordination["status"]
            if coordination["status"] == "cancelled":
                raise CancelledError("persisted_cancel_completed")
            raise ResumeRecoveryRequiredError(
                "resume_coordination_" + str(coordination.get("status"))
            )
        self.session.owner_lease = self.coordinator.lease
        self.exploration.restore(state.node_timings.get("exploration_checkpoint"))

        # Register the tasks created by the current orchestrator only after
        # recovery has fenced and reconciled resources from an older owner.
        # Otherwise the current running patch task is mistaken for a stale
        # subagent left behind by a crashed process.
        if collaboration is not None:
            collaboration.attach_coordinator(self.coordinator)
            self.session.resource_parent = lambda kind: f"agent:{state.repair_run_id}:" + (
                "verify" if kind == "verify" else "patch"
            )
        self.agent._plan_session = self.session
        request_hash = digest(state.issue_input)
        requests = [e["payload"] for e in self.session.store.events() if e["kind"] == "request"]
        if requests and requests[0]["checksum"] != request_hash:
            raise ValueError("resume_task_objective_mismatch")
        if not requests:
            self.session.store.append("request", {"checksum": request_hash})
        saved_seal = state.node_timings.get("plan_checkpoint")
        if saved_seal:
            self.session.verify_checkpoint(saved_seal)
        self.report = recover(self.session)
        self.publish_recovery(stage="plan")
        if self.report["uncertain"]:
            raise ResumeRecoveryRequiredError("resume_execution_uncertain")
        self._reconcile_replans()
        if context.execution_uncertain or context.sandbox_uncertain:
            if not coordination["resources"]:
                raise ResumeRecoveryRequiredError("resume_untracked_execution_uncertain")
            context.execution_uncertain = False
            context.sandbox_uncertain = False
        if not any(e["kind"] == "baseline" for e in self.session.store.events()):
            before = orchestrator._snapshot_repo()
            self.session.store.append(
                "baseline",
                {
                    "files": {p: self.session.store.put_blob(text) for p, text in before.items()},
                    "workspace": snapshot(orchestrator._repo_root),
                },
            )
        self.operations = self._seed_operations(state)
        planning = [
            e["payload"]
            for e in self.session.store.events()
            if e["kind"] == "planning"
            and self.session.plan
            and e["payload"]["plan_version"] == self.session.plan.plan_version
        ]
        self.conclusion = planning[-1]["conclusion"] if planning else ""
        if not defer_plan:
            self._initialize_plan()
            self.sync()
        self.publish_recovery(stage="context")

    def _event(self, event, payload):
        tracer = getattr(getattr(self.orchestrator, "_repair_ctx", None), "repair_tracer", None)
        if tracer:
            tracer.emit("plan", event, payload)
        if event in {
            "node_started",
            "node_succeeded",
            "node_failed",
            "node_uncertain",
            "node_blocked",
            "node_stale",
            "plan_created",
            "planning_started",
            "replan_decided",
            "replan_started",
            "replan_rejected",
            "replan_committed",
            "plan_resume_reconciled",
        }:
            view = (
                self.session.plan_view(payload.get("node_id", ""))
                if self.session and self.session.plan
                else {}
            )
            node = next(
                (n for n in view.get("nodes", []) if n["node_id"] == payload.get("node_id")), {}
            )
            phase = node.get("kind", "replan" if event.startswith("replan_") else "plan")
            self.orchestrator._progress_emitter().emit(
                "plan_progress",
                summary=f"{phase}: {event} {payload.get('node_id', '')}",
                phase=phase,
                plan_view=view,
                action=payload.get("action", ""),
                reason=payload.get("reason", ""),
                trigger_ref=payload.get("trigger_ref", ""),
            )

    def _exploration_event(self, event, payload):
        emitter = getattr(self.agent, "_turn_event_emitter", None)
        if emitter is not None:
            emitter.emit(
                event,
                **{
                    k: v
                    for k, v in payload.items()
                    if k not in {"event", "event_seq", "run_id", "turn_id"}
                },
            )
        tracer = getattr(getattr(self.orchestrator, "_repair_ctx", None), "repair_tracer", None)
        if tracer:
            tracer.emit("exploration", event, payload)
        self.orchestrator._progress_emitter().emit(
            "exploration_progress", summary=f"{event}: {payload.get('task_id', '')}"
        )

    def sync(self):
        seal = self.session.checkpoint()
        self.state.node_timings["plan_checkpoint"] = seal
        if self.exploration is not None:
            self.state.node_timings["exploration_checkpoint"] = self.exploration.checkpoint()
            self.state.node_timings["exploration_progress"] = self.exploration.progress()
        view = self.session.plan_view()
        self.state.node_timings["plan_progress"] = {
            **view,
            "version": view["plan_version"],
            "nodes": [
                {
                    **n,
                    "id": n["node_id"],
                    "reason": n["block_or_failure_reason"],
                }
                for n in view["nodes"]
            ],
            "resume": self.report,
        }

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._cancel_unsubscribe is not None:
            self._cancel_unsubscribe()
        self._heartbeat_stop.set()
        if self._heartbeat_worker is not None:
            self._heartbeat_worker.join(timeout=1)
        try:
            if self.exploration is not None:
                try:
                    self.exploration.close()
                except ValueError:
                    self.coordinator.finish(
                        "recovery_required", error_code="exploration_cleanup_unconfirmed"
                    )
                    self.state.set_status("recovery_required", "exploration_cleanup_unconfirmed")
            current = self.coordinator.store.snapshot(self.state.repair_run_id)
            if current.owner_token == self.coordinator.lease.owner_token and current.owner_token:
                if current.status == "active":
                    current = self.coordinator.finish("released")
                elif current.status in {"reconciling", "cancel_requested", "cancelling"}:
                    current = self.coordinator.finish(
                        "recovery_required", error_code="binding_closed_before_cleanup"
                    )
                self.state.node_timings["coordination_status"] = current.status
                if current.status == "recovery_required":
                    self.state.set_status("recovery_required", "cleanup_or_write_unconfirmed")
            if current.generation == self.coordinator.lease.generation:
                previous = self.state.recovery_outcome
                self.publish_recovery(
                    stage=previous.get("stage", "resources"),
                    reason_code=previous.get("reason_code", ""),
                )
        finally:
            if getattr(self.agent, "_plan_session", None) is self.session:
                self.agent._plan_session = None
            if self.agent.tool_context.run_coordinator is self.coordinator:
                self.agent.tool_context.run_coordinator = None
            if (
                getattr(self.agent.tool_context, "readonly_exploration_runtime", None)
                is self.exploration
            ):
                self.agent.tool_context.readonly_exploration_runtime = None
            if (
                self.exploration is not None
                and getattr(self.agent, "_run_budget_manager", None) is self.exploration.budget
            ):
                del self.agent._run_budget_manager
            if self.session is not None:
                self.session.close()

    def _seed_operations(self, state):
        paths = list(
            dict.fromkeys(
                [s.file_path for s in state.suspect_locations]
                + list(state.repair_plan.suspect_files if state.repair_plan else [])
            )
        )[:2]
        paths = [p for p in paths if (Path(self.session.workspace) / p).is_file()]
        if paths and "read_file" in self.agent._tool_names:
            return [{"tool": "read_file", "arguments": {"path": p}} for p in paths]
        return [{"tool": "list_files", "arguments": {"path": "."}}]

    def _isolated_read(self, operation, attempt):
        # Registry callbacks close over ToolContext: copying a dictionary alone
        # would still race on the original context. Rebuild them around a child.
        child = copy.copy(self.agent)
        child.session = {"id": self.session.identity["session_id"]}
        parent_token = getattr(self.agent, "cancel_token", None)
        child.cancel_token = _ReadCancellationToken(parent_token)
        child._repair_budget = None
        if parent_token is not None:
            parent_token.check()
        child.tool_context = replace(
            self.agent.tool_context,
            cancel_token=child.cancel_token,
            edit_lock=None,
            observation_state=child.session,
            exploration_service=None,
            path_resolver=None,
        )
        child.tools = build_repair_canonical_tools(child.tool_context)
        child._tool_names = {operation["tool"]}
        child.quota = QuotaEnforcer(
            max_total=1, max_writes=0, max_shell=0, group_limits={"read": 1}
        )
        if hasattr(child, "_tool_executor"):
            del child._tool_executor
        child._plan_session = self.session
        result = child.execute_tool(operation["tool"], operation["arguments"])
        if not result.ok:
            return {
                "status": "failed",
                "reason": result.error_code,
                "evidence_refs": self.session.tool_result(attempt)["evidence_refs"],
            }
        # Mutate the parent's read-before-write ledger only on the owner thread
        # after the scheduler has joined both isolated reads.
        return self.session.tool_result(attempt)

    def _initialize_plan(self):
        session = self.session
        if session.plan is not None:
            return
        refs = []
        for operation in self.operations:
            self.read_budget.reserve(1)
            result = session.preplan_read(
                lambda attempt, op=operation: self._isolated_read(op, attempt),
                (operation["tool"],),
            )
            refs.extend(r for r in result.get("evidence_refs", ()) if session.evidence.valid(r))
        if not refs:
            raise ValueError("preplan_repository_evidence_missing")
        session.emit("planning_started")
        plan, self.conclusion = grounded_plan(
            session,
            self.operations,
            refs,
            objective=self.state.issue_input,
            light_client=self._planning_client(),
        )
        session.create(plan)
        session.configure_long_task(
            self.state.issue_input, hard_constraints=self.state.hard_constraints
        )
        # Persist before entering any modifying tool loop.
        self.state.node_timings["plan_checkpoint"] = session.checkpoint()
        self.orchestrator._checkpoint_progress(self.state)

    def _planning_client(self):
        client = getattr(self.agent, "light_client", None) or self.agent.model_client

        def complete(prompt, max_new_tokens):
            decisions = self.exploration.budget.reserve_many(
                {
                    "llm_calls": 1,
                    "prompt_tokens": len(prompt.encode("utf-8")),
                }
            )
            if any(not decision.allowed for decision in decisions):
                raise ValueError("plan_generation_global_budget_exhausted")
            deadline = getattr(self.agent, "_repair_deadline", None)
            remaining = deadline.remaining_s() if deadline else None
            if remaining is not None and remaining <= 0:
                raise ValueError("plan_generation_deadline_exceeded")
            timeout = min(60, remaining) if remaining is not None else 60
            return run_blocking(
                lambda: client.complete(prompt, max_new_tokens=max_new_tokens),
                cancel_token=getattr(self.agent, "cancel_token", None),
                timeout_s=timeout,
            )

        return SimpleNamespace(complete=complete)

    def _kind(self, kind):
        return next(n for n in self.session.plan.nodes if n.kind == kind)

    def _reconcile_replans(self):
        """A saved Plan proves commit even if the following trace was lost."""
        session = self.session
        events = session.store.events()
        for request in session.store.latest("replan_request", "trigger_ref").values():
            if request["status"] != "started":
                continue
            committed = next(
                (
                    e["payload"]
                    for e in events
                    if e["kind"] == "plan"
                    and e["payload"].get("plan_version") == request["plan_version"] + 1
                    and e["payload"].get("parent_plan_checksum") == request["plan_checksum"]
                    and e["payload"].get("replan_reason")
                    == "verification_failed:" + request["trigger_ref"]
                ),
                None,
            )
            if committed is None:
                raise ResumeRecoveryRequiredError("replan_attempt_outcome_unconfirmed")
            session.store.append(
                "replan_request",
                {**request, "status": "committed", "committed_version": committed["plan_version"]},
            )
            if not any(
                e["kind"] == "trace"
                and e["payload"].get("event") == "replan_committed"
                and e["payload"].get("trigger_ref") == request["trigger_ref"]
                for e in events
            ):
                session.emit(
                    "replan_committed",
                    trigger_ref=request["trigger_ref"],
                    reason="recovered_durable_commit",
                )

    def _verification_trigger(self):
        from src.state import VerificationResult

        session = self.session
        node = self._kind("verify")
        attempt = session.store.latest("attempt", "attempt_id").get(node.attempt_id, {})
        result = attempt.get("result", {})
        if node.status != "failed" or result.get("status") != "failed":
            return "", None, {}, "", {}
        for ref in result.get("evidence_refs", []):
            record = session.evidence.get(ref)
            if not record or record.get("kind") != "tests_passed":
                continue
            receipt = record.get("receipt", {})
            if (
                record["attempt_id"] != node.attempt_id
                or receipt.get("attempt_id") != node.attempt_id
                or receipt.get("run_id") != session.identity["run_id"]
                or not record.get("execution_stopped")
                or attempt.get("plan_version") != session.plan.plan_version
                or attempt.get("phase") != "reconciled"
            ):
                continue
            trigger = digest(
                [session.identity["run_id"], session.plan.plan_version, node.attempt_id, ref]
            )
            return (
                trigger,
                VerificationResult.from_dict(result["verification_result"]),
                receipt,
                ref,
                record["file_versions"],
            )
        raise ResumeRecoveryRequiredError("verification_failure_receipt_missing")

    def _replan_safety_reason(self):
        session = self.session
        token = getattr(self.agent, "cancel_token", None)
        if token and token.is_cancelled:
            return "replan_cancelled"
        context = self.agent.tool_context
        if context.execution_uncertain or context.sandbox_uncertain:
            return "workspace_execution_uncertain"
        current = self.coordinator.store.snapshot(self.state.repair_run_id)
        if current.status != "active":
            return "replan_coordination_" + current.status
        if any(
            r.kind in {"plan_attempt", "exploration_task", "sandbox_call"}
            and r.cleanup != "confirmed"
            for r in current.resources
        ):
            return "replan_resource_cleanup_unconfirmed"
        try:
            check_replan(session)
        except ValueError as exc:
            if str(exc) != "replan_budget_exceeded":
                return str(exc)
        return ""

    def _fresh_source_refs(self):
        session = self.session
        read_ops = {
            r
            for op in session.store.latest("operation", "operation_id").values()
            if op["effect"] == "read"
            and op["phase"] == "result_recorded"
            and op.get("execution_stopped")
            for r in op.get("evidence_refs", [])
        }
        return [
            r
            for r in sorted(read_ops)
            if (session.evidence.get(r) or {}).get("kind") == "observation_present"
            and session.evidence.valid(r)
        ]

    def _refresh_replan_evidence(self):
        """Use existing fixed explore nodes and their authorized read path."""
        session = self.session
        for node in session.plan.nodes:
            if node.kind != "explore" or node.status not in {"stale", "succeeded", "ready"}:
                continue
            self.read_budget.reserve(1)
            if node.status == "succeeded":
                session.commit(
                    transition(
                        session.plan,
                        node.node_id,
                        "stale",
                        attempt_id=node.attempt_id,
                        failure="replan_source_refresh",
                    )
                )
            if session.plan.node(node.node_id).status == "stale":
                session.commit(
                    transition(session.plan, node.node_id, "ready", attempt_id=node.attempt_id)
                )
            operation = {"tool": node.tool_name, "arguments": json.loads(node.arguments_json)}
            session.run_node(node.node_id, lambda a, op=operation: self._isolated_read(op, a))

    def _retry_from_verification(self):
        session = self.session
        self._reconcile_replans()
        trigger, result, receipt, ref, failed_workspace = self._verification_trigger()
        handled = session.store.latest("replan_request", "trigger_ref").get(trigger)
        if handled:
            raise ValueError("replan_trigger_already_processed:" + handled["status"])
        safety = self._replan_safety_reason()
        deadline = getattr(self.agent, "_repair_deadline", None)
        stop = "stop_loss" if has_stop_loss(self.state) else ""
        if deadline and deadline.remaining_s() is not None and deadline.remaining_s() <= 0:
            stop = "plan_generation_deadline_exceeded"
        if any(
            self.exploration.budget.remaining(k) is not None
            and self.exploration.budget.remaining(k) < 1
            for k in ("llm_calls", "prompt_tokens")
        ):
            stop = "plan_generation_global_budget_exhausted"
        refs = self._fresh_source_refs()

        def evaluate():
            decision = decide_replan(
                session.plan_view(),
                trigger_ref=trigger,
                result=result,
                receipt=receipt,
                evidence_refs=refs,
                safety_reason=safety,
                stop_reason=stop,
                retry_allowed=0 < self.state.retry_count < self.state.max_retries,
            )
            session.emit("replan_decided", **decision.to_dict())
            return decision

        decision = evaluate()
        if decision.action == "needs_evidence":
            self._refresh_replan_evidence()
            refs = self._fresh_source_refs()
            safety = self._replan_safety_reason()
            decision = evaluate()
        if decision.action != "replan":
            raise ValueError("replan_" + decision.action + ":" + decision.reason)
        check_replan(session)
        context = {
            "old_plan": session.plan_view(),
            "trigger_node_id": self._kind("verify").node_id,
            "trigger_ref": trigger,
            "verification_evidence_ref": ref,
            "verification_receipt": receipt,
            "failure_excerpt": collect_log_excerpt(result),
            "failed_workspace": failed_workspace,
            "current_workspace": snapshot(session.workspace),
            "fresh_source_evidence_refs": refs,
        }
        request = {
            "trigger_ref": trigger,
            "plan_version": session.plan.plan_version,
            "plan_checksum": session.plan.plan_checksum,
            "status": "started",
        }
        session.store.append("replan_request", request)
        session.cut("replan_requested")
        session.emit("replan_started", trigger_ref=trigger)

        def before_commit():
            reason = self._replan_safety_reason()
            if reason:
                raise ValueError(reason)
            if session.plan.plan_checksum != request["plan_checksum"]:
                raise ValueError("replan_source_plan_changed")
            if has_stop_loss(self.state):
                raise ValueError("stop_loss")
            if deadline and deadline.remaining_s() is not None and deadline.remaining_s() <= 0:
                raise ValueError("plan_generation_deadline_exceeded")

        try:
            self.conclusion = retry_plan(
                session,
                self.operations,
                refs,
                objective=self.state.issue_input,
                reason="verification_failed:" + trigger,
                light_client=self._planning_client(),
                replan_context=context,
                before_commit=before_commit,
            )
        except Exception as exc:
            # Once a durable candidate exists, recovery must adopt it, never
            # label it rejected because a later trace/checkpoint failed.
            if session.plan.plan_version != request["plan_version"]:
                self._reconcile_replans()
            else:
                session.store.append(
                    "replan_request", {**request, "status": "rejected", "reason": str(exc)[:200]}
                )
                session.emit("replan_rejected", trigger_ref=trigger, reason=str(exc)[:200])
            raise
        session.store.append(
            "replan_request",
            {**request, "status": "committed", "committed_version": session.plan.plan_version},
        )

    def _prepare_owner_stages(self):
        session = self.session
        scheduler = PlanScheduler(session, self.read_budget)
        while True:
            session.refresh()
            ready = [n for n in session.plan.nodes if n.kind == "explore" and n.status == "ready"]
            if not ready:
                break
            callbacks = {}
            for node in ready:
                operation = {"tool": node.tool_name, "arguments": json.loads(node.arguments_json)}
                existing = [
                    v["evidence_id"]
                    for v in session.store.latest("evidence", "evidence_id").values()
                    if v["kind"] == "observation_present"
                    and session.evidence.valid(v["evidence_id"])
                ]
                # Reuse a matching pre-plan observation only, never an unrelated read.
                matching = []
                for ref in existing:
                    for op in session.store.latest("operation", "operation_id").values():
                        if (
                            ref in op.get("evidence_refs", ())
                            and op["tool"] == operation["tool"]
                            and op["args_hash"] == digest(operation["arguments"])
                        ):
                            matching.append(ref)
                if matching:
                    session.run_node(
                        node.node_id,
                        lambda a, refs=matching: {
                            "status": "success",
                            "evidence_refs": refs,
                        },
                    )
                else:
                    callbacks[node.node_id] = lambda a, op=operation: self._isolated_read(op, a)
            if callbacks:
                scheduler.run_reads(callbacks)
        analysis = self._kind("analyze")
        if analysis.status == "ready":
            if not self.conclusion:
                raise ValueError("structured_analysis_required_before_edit")
            refs = [
                r
                for n in session.plan.nodes
                if n.kind == "explore"
                for r in n.output_evidence_refs
                if session.evidence.valid(r)
            ]
            session.run_node(analysis.node_id, lambda a: session.analysis(a, self.conclusion, refs))

    def run_patcher(self, prompt, metadata, callback):
        session = self.session
        if session.plan is None:
            self.operations = self._seed_operations(self.state)
            self._initialize_plan()
            self.sync()
        edit = self._kind("edit")
        rollback = [
            e["payload"]
            for e in session.store.events()
            if e["kind"] == "rollback"
            and e["payload"].get("plan_version") == session.plan.plan_version
        ]
        rolled_back = bool(rollback and rollback[-1].get("completed"))
        verification_failed = self._kind("verify").status == "failed"
        if edit.status == "succeeded" and not rolled_back and not verification_failed:
            baseline = next(e["payload"] for e in session.store.events() if e["kind"] == "baseline")
            before = {p: session.store.get_blob(key) for p, key in baseline["files"].items()}
            from src.repair.execution.edit_from_disk import patches_from_snapshot_diff

            return patches_from_snapshot_diff(before, self.orchestrator._snapshot_repo()), {
                "total_ms": 0,
                "model_call_ms": 0,
                "parse_apply_ms": 0,
                "edit_mode": "plan_resume_adopted",
                "agent_answer": "adopted durable patch",
            }
        if edit.status in {"failed", "cancelled", "blocked"} or rolled_back or verification_failed:
            self._retry_from_verification()
        self._prepare_owner_stages()
        edit = self._kind("edit")
        if edit.status != "ready":
            raise ValueError("plan_edit_dependency_not_satisfied")
        value = []

        def execute(attempt):
            try:
                value.append(callback(prompt + "\n\nPlan evidence: " + self.conclusion, metadata))
            finally:
                self.exploration.drain(cancel=True)
            return session.tool_result(attempt)

        session.run_node(edit.node_id, execute)
        self.sync()
        if self._kind("edit").status != "succeeded":
            raise ValueError("plan_edit_completion_not_confirmed")
        return value[0]

    def run_verifier(self, callback):
        from src.state import VerificationResult

        session = self.session
        self.exploration.before_write()
        verify = self._kind("verify")
        if verify.status == "succeeded":
            attempts = session.store.latest("attempt", "attempt_id")
            return VerificationResult.from_dict(
                attempts[verify.attempt_id]["result"]["verification_result"]
            )
        value = []

        def execute(attempt):
            result = callback()
            value.append(result)
            receipt = self.state.node_timings.get("plan_verification_receipt", {})
            return session.verify_result(attempt, result.to_dict(), receipt)

        session.run_node(verify.node_id, execute)
        self.sync()
        if self._kind("verify").status == "uncertain":
            raise ValueError("plan_verification_execution_unconfirmed")
        if not value:
            raise ValueError("plan_verification_execution_unconfirmed")
        # Plan completion and L2 terminal status must agree.
        if value[0].all_passed and self._kind("verify").status != "succeeded":
            return VerificationResult(
                all_passed=False, failure_logs=["plan_completion_unsatisfied"]
            )
        return value[0]

    def rollback(self, callback, expected_after):
        session = self.session
        self.exploration.drain(cancel=True)
        session._fence(allow_cancelling=True)
        if session.plan and any(n.status in {"running", "uncertain"} for n in session.plan.nodes):
            raise ValueError("rollback_conflicts_with_unconfirmed_execution")
        payload = {
            "plan_version": session.plan.plan_version if session.plan else 0,
            "workspace_before": snapshot(session.workspace),
            "expected_after": expected_after,
            "completed": False,
        }
        session.store.append("rollback", payload)
        session.cut("rollback_dispatched")
        callback()
        after = snapshot(session.workspace)
        session.store.append(
            "rollback", {**payload, "workspace_after": after, "completed": after == expected_after}
        )
        session.cut("rollback_recorded")
        if after != expected_after:
            raise ValueError("rollback_outcome_unconfirmed")
