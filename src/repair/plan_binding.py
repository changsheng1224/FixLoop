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
from agent_runtime.plan_runtime.scheduler import PlanScheduler, ReadBudget
from agent_runtime.plan_runtime.session import PlanSession
from agent_runtime.plan_runtime.workspace import snapshot
from agent_runtime.run_coordination import CoordinationError, RunCoordinator
from agent_runtime.tool_executor import QuotaEnforcer
from src.tools.composite import build_repair_canonical_tools


class ResumeRecoveryRequiredError(CoordinationError):
    """Resume is fenced until all old writes and resources are confirmed."""

    def __init__(self, reason: str):
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
        self.agent = orchestrator.patcher
        self.session = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat_worker = None
        self.coordinator.cancel_token = getattr(orchestrator.patcher, "cancel_token", None)
        self._cancel_unsubscribe = None
        try:
            if self.coordinator.cancel_token is not None:
                self._cancel_unsubscribe = self.coordinator.cancel_token.add_callback(
                    self.coordinator.request_cancel
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
            finally:
                self.close()
            raise

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
        if coordination.get("status") != "active":
            state.node_timings["coordination_status"] = coordination["status"]
            if coordination["status"] == "cancelled":
                raise CancelledError("persisted_cancel_completed")
            raise ResumeRecoveryRequiredError(
                "resume_coordination_" + str(coordination.get("status"))
            )
        self.session.owner_lease = self.coordinator.lease

        def maintain_owner():
            while not self._heartbeat_stop.wait(self.coordinator.lease_seconds / 3):
                try:
                    self.coordinator.heartbeat()
                except Exception:
                    return  # the durable fence rejects further dispatch

        self._heartbeat_worker = threading.Thread(target=maintain_owner, daemon=True)
        self._heartbeat_worker.start()
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
        if self.report["uncertain"]:
            raise ResumeRecoveryRequiredError("resume_execution_uncertain")
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
            "replan_committed",
            "plan_resume_reconciled",
        }:
            self.orchestrator._progress_emitter().emit(
                "plan_progress", summary=f"{event}: {payload.get('node_id', '')}"
            )

    def sync(self):
        seal = self.session.checkpoint()
        self.state.node_timings["plan_checkpoint"] = seal
        self.state.node_timings["plan_progress"] = {
            "version": self.session.plan.plan_version,
            "nodes": [
                {
                    "id": n.node_id,
                    "kind": n.kind,
                    "status": n.status,
                    "depends_on": list(n.depends_on),
                    "reason": n.failure,
                }
                for n in self.session.plan.nodes
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
        finally:
            if getattr(self.agent, "_plan_session", None) is self.session:
                self.agent._plan_session = None
            if self.agent.tool_context.run_coordinator is self.coordinator:
                self.agent.tool_context.run_coordinator = None
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
        plan, self.conclusion = grounded_plan(
            session,
            self.operations,
            refs,
            objective=self.state.issue_input,
            light_client=self._planning_client(),
        )
        session.create(plan)
        # Persist before entering any modifying tool loop.
        self.state.node_timings["plan_checkpoint"] = session.checkpoint()
        self.orchestrator._checkpoint_progress(self.state)

    def _planning_client(self):
        client = getattr(self.agent, "light_client", None) or self.agent.model_client

        def complete(prompt, max_new_tokens):
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
        if edit.status == "succeeded" and not rolled_back:
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
        if edit.status in {"failed", "cancelled", "blocked"} or rolled_back:
            refs = [
                v["evidence_id"]
                for v in session.store.latest("evidence", "evidence_id").values()
                if v["kind"] == "observation_present" and session.evidence.valid(v["evidence_id"])
            ]
            if not refs:
                raise ValueError("replan_requires_fresh_repository_evidence")
            self.conclusion = retry_plan(
                session,
                self.operations,
                refs,
                objective=self.state.issue_input,
                reason="orchestrator_retry",
                light_client=self._planning_client(),
            )
        self._prepare_owner_stages()
        edit = self._kind("edit")
        if edit.status != "ready":
            raise ValueError("plan_edit_dependency_not_satisfied")
        value = []

        def execute(attempt):
            value.append(callback(prompt + "\n\nPlan evidence: " + self.conclusion, metadata))
            return session.tool_result(attempt)

        session.run_node(edit.node_id, execute)
        self.sync()
        if self._kind("edit").status != "succeeded":
            raise ValueError("plan_edit_completion_not_confirmed")
        return value[0]

    def run_verifier(self, callback):
        from src.state import VerificationResult

        session = self.session
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
