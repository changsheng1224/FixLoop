"""Plan owner, durable dispatch boundary and tool receipt capture."""

from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path

from agent_runtime.context_runtime import ObservationStore
from agent_runtime.state_root import state_root_for
from agent_runtime.tool_result import ToolResult, attach_tool_receipt, normalize_tool_result

from .evidence import EvidenceLedger
from .journal import PlanStore
from .models import NodeAttempt, Plan, digest, new_id
from .processes import process_identity
from .reducer import refresh, transition
from .validate import tool_effect, validate_plan
from .workspace import changes, snapshot, workspace_id, workspace_lease
from .long_task import LongTaskContext, LongTaskState


class PlanSession:
    def __init__(
        self,
        workspace: str,
        task_id: str,
        run_id: str,
        registry: dict,
        *,
        state_root: str = "",
        observation_state: dict | None = None,
        event_sink=None,
        fault=None,
    ):
        self.workspace = str(Path(workspace).resolve())
        self.registry = registry
        self.state_root = state_root
        self.identity = {
            "task_id": task_id,
            "run_id": run_id,
            "workspace_id": workspace_id(workspace),
            "session_id": "plan-" + digest([task_id, run_id])[:24],
        }
        base = state_root_for(workspace, state_root) / ".agent" / "plans"
        # Lease identity follows the workspace, independently of journal location.
        lease_root = Path(tempfile.gettempdir()) / "fixloop-plan-leases"
        self._lease = workspace_lease(lease_root / (self.identity["workspace_id"] + ".lock"))
        self._lease.__enter__()
        try:
            self.store = PlanStore(base / digest([task_id, run_id]), self.identity)
            self.plan = self.store.load_plan()
            if self.plan:
                validate_plan(self.plan, registry, identity=self.identity)
        except BaseException:
            if hasattr(self, "store"):
                self.store.close()
            self._lease.__exit__(None, None, None)
            raise
        self.observation_state = observation_state if observation_state is not None else {}
        self.evidence = EvidenceLedger(self.store, workspace, self.observation_state, state_root)
        raw_long_task = self.store.latest("long_task_state", "state_id")
        latest_long_task = next(iter(raw_long_task.values()), None)
        self.long_task_state = LongTaskState.verify(latest_long_task) if latest_long_task else LongTaskState(task_id=task_id, run_id=run_id)
        self.long_task = LongTaskContext(self.long_task_state, self.plan, self.evidence)
        self.event_sink = event_sink
        self.fault = fault
        self.local = threading.local()
        self._mutex = threading.RLock()
        self.owner_thread = threading.get_ident()
        self._closed = False

    def close(self):
        if not self._closed:
            self._closed = True
            self.store.close()
            self._lease.__exit__(None, None, None)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def cut(self, point: str):
        if self.fault:
            self.fault(point)

    def emit(self, event: str, **payload):
        body = {
            **self.identity,
            "plan_id": self.plan.plan_id if self.plan else "",
            "plan_version": self.plan.plan_version if self.plan else 0,
            "state_revision": self.plan.state_revision if self.plan else 0,
            **payload,
        }
        self.store.append("trace", {"event": event, **body})
        if self.event_sink:
            self.event_sink(event, body)

    def commit(self, plan: Plan):
        validate_plan(plan, self.registry, identity=self.identity)
        self.store.save_plan(plan)
        self.plan = plan
        self.cut("plan_saved")

    def create(self, plan: Plan):
        if self.plan is not None:
            raise ValueError("plan_already_created")
        if any(
            n.status != "pending" or n.attempt_id or n.output_evidence_refs or n.receipt_refs
            for n in plan.nodes
        ):
            raise ValueError("initial_plan_cannot_claim_execution")
        self.commit(plan)
        self.emit("plan_created", nodes=[n.definition() for n in plan.nodes])
        self.checkpoint()

    def checkpoint(self) -> dict:
        self.cut("before_checkpoint")
        self._persist_long_task_state()
        seal = self.store.checkpoint(self.plan, self.long_task_state.seal())
        self.cut("checkpoint_saved")
        return seal

    def _persist_long_task_state(self) -> None:
        raw = self.long_task_state.seal()
        raw["state_id"] = self.identity["session_id"]
        self.store.append("long_task_state", raw)

    def configure_long_task(self, original_request: str, *, hard_constraints=None) -> None:
        self.long_task_state.original_request = str(original_request)
        self.long_task_state.hard_constraints = [str(v) for v in (hard_constraints or [])]
        self.long_task._touch()
        self._persist_long_task_state()

    def record_decision(self, decision: str, *, rationale: str = "", source: str = "") -> None:
        self.long_task.record_decision(decision, rationale=rationale, source=source)
        self._persist_long_task_state()

    def build_long_task_context(self, node_id: str = "") -> dict:
        self.long_task.plan = self.plan
        return self.long_task.build(node_id)

    def render_long_task_context(self, node_id: str = "") -> str:
        self.long_task.plan = self.plan
        return self.long_task.render(node_id)

    def verify_long_task_checkpoint(self, checkpoint: dict) -> None:
        self.store.verify_checkpoint(checkpoint)
        LongTaskState.verify(checkpoint.get("long_task_state") or {})

    def refresh_evidence(self, ref: str, fetcher) -> str:
        """Refresh a stale evidence ref through a caller-supplied fetcher."""
        new_ref = self.long_task.refresh_evidence(ref, fetcher)
        self._persist_long_task_state()
        return new_ref

    def refresh_observation_with_node(self, ref: str, node_id: str, callback) -> str:
        """Re-fetch stale evidence through a newly scheduled Plan explore node."""
        if not self.evidence.get(ref):
            raise ValueError("unknown_evidence_ref")
        if ref not in self.long_task_state.evidence_refs:
            self.long_task_state.evidence_refs.append(ref)
        if self.evidence.valid(ref):
            return ref
        result = self.run_node(node_id, callback)
        refs = list(result.get("evidence_refs", [])) if isinstance(result, dict) else []
        candidates = [item for item in refs if self.evidence.valid(item)]
        if not candidates:
            raise ValueError("refresh_node_missing_valid_evidence")
        new_ref = candidates[0]
        self.long_task_state.evidence_refs = [new_ref if item == ref else item for item in self.long_task_state.evidence_refs]
        self.long_task_state.stale_evidence = [item for item in self.long_task_state.stale_evidence if item != ref]
        self.long_task_state.key_decisions.append({"id": new_id("evidence"), "supersedes": ref, "replacement": new_ref})
        self.long_task_state.state_revision += 1
        self._persist_long_task_state()
        return new_ref

    def refresh(self):
        candidate = refresh(self.plan, self.evidence)
        if candidate.plan_checksum != self.plan.plan_checksum:
            old = self.plan
            self.commit(candidate)
            for node in candidate.nodes:
                if node.status != old.node(node.node_id).status:
                    self.emit("node_" + node.status, node_id=node.node_id, reason=node.failure)

    def prepare(self, node_id: str) -> dict:
        with self._mutex:
            self.refresh()
            node = self.plan.node(node_id)
            if node.status != "ready" or any(n.status == "uncertain" for n in self.plan.nodes):
                raise ValueError("node_not_ready_or_workspace_uncertain")
            running = [n for n in self.plan.nodes if n.status == "running"]
            if running and (
                node.kind != "explore"
                or len(running) >= 2
                or any(n.kind != "explore" for n in running)
            ):
                raise ValueError("node_execution_conflict")
            if node.kind != "explore" and threading.get_ident() != self.owner_thread:
                raise ValueError("owner_thread_required")
            attempt = NodeAttempt(
                new_id("attempt"),
                self.plan.plan_id,
                self.plan.plan_version,
                node_id,
                self.plan.state_revision,
                snapshot(self.workspace),
                node.tool_allowlist,
                new_id("dispatch"),
                kind=node.kind,
                owner=process_identity(os.getpid()) or {},
            )
            self.long_task.set_node(node_id, "running")
            self._persist_long_task_state()
            raw = asdict(attempt)
            self.store.append("attempt", raw)
            self.cut("prepared")
            self.commit(transition(self.plan, node_id, "running", attempt_id=attempt.attempt_id))
            self.store.append("attempt", {**raw, "phase": "dispatched"})
            self.cut("dispatched")
            self.emit("node_started", node_id=node_id, attempt_id=attempt.attempt_id)
            return {**raw, "phase": "dispatched"}

    def preplan_read(self, callback, allowed_tools: tuple[str, ...]) -> dict:
        if self.plan is not None:
            raise ValueError("preplan_after_plan_created")
        attempts = self.store.latest("attempt", "attempt_id")
        if len([a for a in attempts.values() if a["plan_version"] == 0]) >= 4:
            raise ValueError("preplan_budget_exceeded")
        attempt = asdict(
            NodeAttempt(
                new_id("attempt"),
                "preplan",
                0,
                new_id("preplan"),
                0,
                snapshot(self.workspace),
                allowed_tools,
                new_id("dispatch"),
                owner=process_identity(os.getpid()) or {},
            )
        )
        self.store.append("attempt", attempt)
        self.cut("prepared")
        attempt = {**attempt, "phase": "dispatched"}
        self.store.append("attempt", attempt)
        self.cut("dispatched")
        result = self.invoke(attempt, callback)
        operations = self.operations(attempt["attempt_id"])
        if any(not op.get("execution_stopped") for op in operations):
            raise ValueError("preplan_execution_uncertain")
        self.store.append(
            "attempt", {**result, "phase": "reconciled", "terminal_status": "succeeded"}
        )
        self.checkpoint()
        return result["result"]

    def execute_tool(self, agent, name: str, arguments: dict, raw_execute):
        """Called inside Agent.execute_tool, encompassing Gateway and Executor gates."""
        attempt = getattr(self.local, "attempt", None)
        if attempt is None:
            raise ValueError("plan_tool_outside_active_attempt")
        if name not in attempt["allowed_tools"]:
            return ToolResult(
                content=f"Error: Plan node disallows {name}",
                status="rejected",
                error_code="policy_denied",
            )
        effect = tool_effect(name, self.registry)
        if attempt["kind"] == "explore" and effect != "read":
            raise ValueError("readonly_tool_violation")
        operation_id = new_id("op")
        call_id = new_id("call")
        before = snapshot(self.workspace)
        prior_operations = self.operations(attempt["attempt_id"])
        expected = (
            prior_operations[-1].get("workspace_after")
            if prior_operations
            else attempt["workspace_before"]
        )
        if expected != before:
            raise ValueError("external_workspace_change_requires_replan")
        operation = {
            "operation_id": operation_id,
            "attempt_id": attempt["attempt_id"],
            "plan_version": attempt["plan_version"],
            "node_id": attempt["node_id"],
            "call_id": call_id,
            "tool": name,
            "args_hash": digest(arguments),
            "effect": effect,
            "workspace_before": before,
            "phase": "prepared",
        }
        with self._mutex:
            if len(self.store.latest("operation", "operation_id")) >= 50:
                raise ValueError("plan_tool_budget_exceeded")
            self.store.append("operation", operation)
        self.cut("tool_prepared")
        if effect != "read" and any(n.status == "uncertain" for n in self.plan.nodes):
            raise ValueError("workspace_execution_uncertain")
        if before != snapshot(self.workspace):
            raise ValueError("workspace_changed_before_dispatch")
        # Bind the canonical identity consumed by the existing ToolExecutor.
        pending = agent.session.get("_pending_canonical_tool_call", {})
        agent.session["_pending_canonical_tool_call"] = {**pending, "call_id": call_id}
        self.store.append("operation", {**operation, "phase": "dispatched"})
        self.cut("tool_dispatched")
        result = normalize_tool_result(raw_execute(), tool_name=name)
        if not result.receipt:
            result = attach_tool_receipt(
                result,
                name,
                args_hash=digest(arguments),
                run_id=self.identity["run_id"],
                call_id=call_id,
            )
        after = snapshot(self.workspace)
        receipt = result.receipt
        if (
            receipt.get("call_id") != call_id
            or receipt.get("run_id") != self.identity["run_id"]
            or receipt.get("tool") != name
        ):
            raise ValueError("tool_receipt_identity_mismatch")
        changed = changes(before, after)
        stopped = result.metadata.get("termination_guaranteed", True) is True
        if result.error_code in {"tool_timeout", "tool_cancelled", "deadline_exceeded"}:
            stopped = result.metadata.get("termination_guaranteed") is True
        if result.status == "uncertain":
            stopped = False
        state = {
            "id": self.identity["session_id"],
            "run_id": self.identity["run_id"],
            "session_scope": {"session_id": self.identity["session_id"]},
        }
        observations = ObservationStore(state, self.workspace, self.state_root)
        try:
            stored = observations.put(
                name,
                arguments,
                result.content,
                status=result.status,
                provenance={"call_id": call_id, "plan_attempt_id": attempt["attempt_id"]},
                source_dependencies=after,
                dependencies=list(after),
            )
            safe_text = observations.expand(stored.observation_id)
        finally:
            observations.close()
        if stored.raw_ref.startswith("memory:"):
            raise ValueError("observation_not_durable")
        versions = (
            after
            if effect != "read" or not arguments.get("path")
            else {p: h for p, h in after.items() if p == str(arguments["path"]).replace("\\", "/")}
        )
        whole = not versions or name != "read_file"
        if whole:
            versions = after
        observation_ref = self.evidence.add(
            "observation_present",
            attempt_id=attempt["attempt_id"],
            status=result.status,
            observation_id=stored.observation_id,
            observation_checksum=stored.checksum,
            observation_record=asdict(stored),
            blob_ref=self.store.put_blob(safe_text),
            file_versions=versions,
            whole_workspace=whole,
            tool_arguments=dict(arguments),
        )
        refs = [observation_ref]
        if effect == "write" and changed and result.ok and stopped:
            refs.append(
                self.evidence.add(
                    "patch_applied",
                    attempt_id=attempt["attempt_id"],
                    status="success",
                    file_versions={p: after.get(p) for p in changed},
                    whole_workspace=False,
                    receipt=result.receipt,
                    changed_paths=changed,
                    execution_stopped=stopped,
                    observation_refs=[observation_ref],
                )
            )
        durable = {
            **operation,
            "phase": "result_recorded",
            "status": result.status,
            "error_code": result.error_code,
            "execution_stopped": stopped,
            "workspace_after": after,
            "changed_paths": changed,
            "evidence_refs": refs,
            "receipt": result.receipt,
            "observation_id": stored.observation_id,
        }
        if effect == "read" and changed:
            durable["execution_stopped"] = False
            durable["error_code"] = "readonly_workspace_changed"
        self.store.append("operation", durable)
        self.cut("tool_result_recorded")
        result.metadata["plan_operation_id"] = operation_id
        result.metadata["plan_attempt_id"] = attempt["attempt_id"]
        result.metadata["observation_id"] = stored.observation_id
        result.metadata["plan_evidence_refs"] = list(refs)
        return result

    def operations(self, attempt_id: str) -> list[dict]:
        return [
            v
            for v in self.store.latest("operation", "operation_id").values()
            if v["attempt_id"] == attempt_id
        ]

    def record_result(self, attempt: dict, result: dict):
        after = snapshot(self.workspace)
        raw = {**attempt, "phase": "result_recorded", "workspace_after": after, "result": result}
        self.store.append("attempt", raw)
        self.cut("result_recorded")
        return raw

    def settle(self, attempt: dict) -> str:
        with self._mutex:
            node = self.plan.node(attempt["node_id"])
            if (
                attempt["plan_version"] != self.plan.plan_version
                or node.attempt_id != attempt["attempt_id"]
            ):
                raise ValueError("late_attempt_result")
            result = attempt["result"]
            refs = tuple(result.get("evidence_refs", ()))
            operations = self.operations(attempt["attempt_id"])
            unknown = any(
                o["phase"] != "result_recorded"
                or not o.get("execution_stopped")
                or (o.get("changed_paths") and o.get("status") != "success")
                for o in operations
            )
            if unknown or result.get("status") == "uncertain":
                status = "uncertain"
            elif result.get("status") == "cancelled":
                status = "cancelled"
            elif result.get("status") == "success" and self.evidence.completion(
                node, refs, attempt["attempt_id"]
            ):
                status = "succeeded"
            else:
                status = "failed"
            if node.status != status:
                self.commit(
                    transition(
                        self.plan,
                        node.node_id,
                        status,
                        attempt_id=attempt["attempt_id"],
                        expected_version=attempt["plan_version"],
                        evidence=self.evidence,
                        output_evidence_refs=refs,
                        receipt_refs=tuple(
                            o["receipt"]["receipt_id"] for o in operations if o.get("receipt")
                        ),
                        failure=""
                        if status == "succeeded"
                        else result.get("reason", "completion_unsatisfied"),
                    )
                )
            self.cut("reducer_saved")
            self.store.append(
                "attempt", {**attempt, "phase": "reconciled", "terminal_status": status}
            )
            self.cut("reconciled")
            if status == "succeeded" and node.kind == "edit":
                for prior in self.plan.nodes:
                    if (
                        prior.kind in {"explore", "analyze"}
                        and prior.status == "succeeded"
                        and not all(self.evidence.valid(ref) for ref in prior.output_evidence_refs)
                    ):
                        self.commit(
                            transition(
                                self.plan,
                                prior.node_id,
                                "stale",
                                attempt_id=prior.attempt_id,
                                failure="patch_changed_source_version",
                            )
                        )
                        self.emit(
                            "node_stale",
                            node_id=prior.node_id,
                            reason="patch_changed_source_version",
                        )
            self.refresh()
            self.long_task.plan = self.plan
            self.long_task.set_node(node.node_id, status)
            self.long_task.add_evidence(list(refs))
            self._persist_long_task_state()
            self.emit(
                "node_" + status,
                node_id=node.node_id,
                attempt_id=attempt["attempt_id"],
                evidence_refs=list(refs),
            )
            self.checkpoint()
            return status

    def invoke(self, attempt: dict, callback) -> dict:
        self.local.attempt = attempt
        try:
            result = callback(attempt)
            return self.record_result(attempt, result)
        finally:
            self.local.attempt = None

    def run_node(self, node_id: str, callback) -> dict:
        attempt = self.prepare(node_id)
        try:
            recorded = self.invoke(attempt, callback)
        except Exception as exc:
            # A callback exception after dispatch is not evidence of no side effects.
            recorded = self.record_result(attempt, {"status": "uncertain", "reason": str(exc)})
        self.settle(recorded)
        return recorded["result"]

    def analysis(self, attempt: dict, conclusion: str, refs: list[str]) -> dict:
        ref = self.evidence.add(
            "analysis_recorded",
            attempt_id=attempt["attempt_id"],
            status="success",
            conclusion=conclusion,
            input_refs=refs,
            file_versions=snapshot(self.workspace),
            whole_workspace=True,
        )
        return {"status": "success", "evidence_refs": [ref]}

    def tool_result(self, attempt: dict) -> dict:
        operations = self.operations(attempt["attempt_id"])
        refs = [r for op in operations for r in op.get("evidence_refs", ())]
        if attempt["kind"] == "edit":
            # Preimage reads stay in the operation history; edit outputs refer
            # only to current, receipt-backed patches.
            refs = [
                r
                for r in refs
                if (self.evidence.get(r) or {}).get("kind") == "patch_applied"
                and self.evidence.valid(r)
            ]
        return {"status": "success", "evidence_refs": refs}

    def verify_result(self, attempt: dict, result: dict, receipt: dict) -> dict:
        receipt = {
            **receipt,
            "run_id": self.identity["run_id"],
            "attempt_id": attempt["attempt_id"],
            "all_passed": result.get("all_passed", False),
            "total_tests": result.get("total_tests", 0),
        }
        ref = self.evidence.add(
            "tests_passed",
            attempt_id=attempt["attempt_id"],
            status="success",
            file_versions=snapshot(self.workspace),
            whole_workspace=True,
            receipt=receipt,
            execution_stopped=receipt.get("completed") is True,
        )
        return {
            "status": "uncertain"
            if receipt.get("completed") is not True
            else "success"
            if result.get("all_passed")
            else "failed",
            "evidence_refs": [ref],
            "verification_result": result,
            "reason": receipt.get("category", "verification_failed"),
        }
