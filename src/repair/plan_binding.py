"""One PlanSession across Patcher asks, orchestration retries and final verification."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

from agent_runtime.cancellation import CancellationToken, run_blocking
from agent_runtime.plan_runtime.models import digest
from agent_runtime.plan_runtime.planner import grounded_plan, retry_plan
from agent_runtime.plan_runtime.recovery import recover
from agent_runtime.plan_runtime.scheduler import PlanScheduler, ReadBudget
from agent_runtime.plan_runtime.session import PlanSession
from agent_runtime.plan_runtime.workspace import snapshot
from agent_runtime.tool_executor import QuotaEnforcer
from src.tools.composite import build_repair_canonical_tools


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
    def __init__(self, orchestrator, state):
        self.orchestrator = orchestrator
        self.state = state
        self.agent = orchestrator.patcher
        self.read_budget = ReadBudget(4)
        context = self.agent.tool_context
        self.session = PlanSession(
            orchestrator._repo_root,
            state.repair_run_id,
            state.repair_run_id,
            self.agent.tools,
            state_root=context.state_root,
            observation_state=self.agent.session,
            event_sink=self._event,
            fault=getattr(orchestrator, "_plan_fault", None),
        )
        self.agent._plan_session = self.session
        request_hash = digest(state.issue_input)
        requests = [e["payload"] for e in self.session.store.events() if e["kind"] == "request"]
        if requests and requests[0]["checksum"] != request_hash:
            self.close()
            raise ValueError("resume_task_objective_mismatch")
        if not requests:
            self.session.store.append("request", {"checksum": request_hash})
        try:
            saved_seal = state.node_timings.get("plan_checkpoint")
            if saved_seal:
                self.session.store.verify_checkpoint(saved_seal)
            self.report = recover(self.session)
            if self.report["uncertain"]:
                raise ValueError("resume_execution_uncertain")
            if not any(e["kind"] == "baseline" for e in self.session.store.events()):
                before = orchestrator._snapshot_repo()
                self.session.store.append(
                    "baseline",
                    {
                        "files": {
                            p: self.session.store.put_blob(text) for p, text in before.items()
                        },
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
            self._initialize_plan()
            self.sync()
        except BaseException:
            self.close()
            raise

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
        self.agent._plan_session = None
        self.session.close()

    def _seed_operations(self, state):
        paths = list(
            dict.fromkeys(
                [s.file_path for s in state.suspect_locations]
                + list(state.repair_plan.suspect_files if state.repair_plan else [])
            )
        )[:2]
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
        if any(n.status in {"running", "uncertain"} for n in session.plan.nodes):
            raise ValueError("rollback_conflicts_with_unconfirmed_execution")
        payload = {
            "plan_version": session.plan.plan_version,
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
