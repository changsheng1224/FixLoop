"""Owner-only structured updates. This module does not interpret natural language."""

import json
import threading
from copy import deepcopy

from .constraint_records import active_constraints, constraint_history, new_constraint
from .decisions import decision_history, new_decision, revoked_decision
from .long_task import LongTaskState
from .models import digest
from .task_messages import TaskMessages


class ContextUpdates:
    """Host API mixed into PlanSession; none of these methods are model tools."""

    def _context_owner(self):
        self._fence()
        if threading.get_ident() != self.owner_thread:
            raise ValueError("owner_thread_required")

    @property
    def task_messages(self):
        return TaskMessages(self.store)

    def register_user_message(self, message_id: str, body: str) -> dict:
        with self._mutex:
            self._context_owner()
            return self.task_messages.register(message_id, body, source_kind="user")

    def register_model_message(self, message_id: str, body: str) -> dict:
        with self._mutex:
            self._context_owner()
            return self.task_messages.register(message_id, body, source_kind="model")

    def source_reference(self, message_id: str, start=0, end=None) -> dict:
        return self.task_messages.reference(message_id, start, end)

    def _validate_context_provenance(self, state=None):
        state = state or self.long_task_state
        if state.task_id != self.identity["task_id"] or state.run_id != self.identity["run_id"]:
            raise ValueError("context_state_identity_mismatch")
        messages = self.task_messages
        if state.configuration_checksum:
            messages.validate([state.original_request_ref], allowed={"user"})
            if (
                state.original_request_ref != messages.reference("original-request")
                or state.original_request_ref["quote"] != state.original_request
            ):
                raise ValueError("original_request_source_mismatch")
            if state.hard_constraints != active_constraints(state.constraint_records):
                raise ValueError("constraint_projection_mismatch")
        for record in constraint_history(state.constraint_records)[0]:
            messages.validate(record["source_refs"], allowed={record["source_kind"]})
        for record in decision_history(state.key_decisions)[0]:
            refs = record.get("source_refs", [])
            if record.get("source_kind") == "user" or refs:
                messages.validate(
                    refs, allowed={"user"} if record["source_kind"] == "user" else {"model"}
                )

    def _assert_context_current(self):
        self._require_context_commit_resolved()
        raw = self.long_task_state.to_dict()
        raw["state_id"] = self.identity["session_id"]
        durable = self.store.latest("long_task_state", "state_id").get(raw["state_id"])
        if durable and durable["state_checksum"] != digest(raw):
            raise ValueError("context_state_mismatch")
        self._validate_context_provenance()

    def _require_context_commit_resolved(self):
        if getattr(self, "_context_commit_pending", False):
            raise ValueError("context_commit_recovery_required")

    def _context_quiescent(self):
        if (
            self.plan
            and any(n.status == "uncertain" for n in self.plan.nodes)
            or any(
                op["phase"] != "result_recorded"
                or not op.get("execution_stopped")
                or (op.get("changed_paths") and op.get("status") != "success")
                for op in self.store.latest("operation", "operation_id").values()
            )
        ):
            raise ValueError("context_execution_not_quiescent")
        local = getattr(self.local, "attempt", None) or {}
        if self.plan and any(
            n.status == "running" and n.node_id != local.get("node_id") for n in self.plan.nodes
        ):
            raise ValueError("context_active_attempt_mismatch")

    def _publish_context(self, state, receipt):
        self._validate_context_provenance(state)
        raw = state.to_dict()
        raw["state_id"] = self.identity["session_id"]
        raw["state_checksum"] = digest(raw)
        receipt = {
            **receipt,
            "state_revision": state.state_revision,
            "context_revision": state.context_revision,
            "state_checksum": raw["state_checksum"],
        }
        receipt["checksum"] = digest(receipt)
        plan_checksum = self.plan.plan_checksum if self.plan else ""
        self.cut("before_context_commit")
        self._context_owner()
        self._assert_context_current()
        self._context_quiescent()
        if plan_checksum != (self.plan.plan_checksum if self.plan else ""):
            raise ValueError("context_plan_version_conflict")
        self._context_commit_pending = True
        self.store.append_many([("context_batch", receipt), ("long_task_state", raw)])
        # A crash here leaves a complete durable batch; retry/resume reads it.
        self.cut("context_committed")
        self.long_task_state = self.long_task.state = state
        self._context_commit_pending = False
        return deepcopy(receipt)

    def _replay_context(self, receipt, checksum):
        if receipt["request_checksum"] != checksum:
            raise ValueError("context_batch_identity_conflict")
        raw = self.store.latest("long_task_state", "state_id")[self.identity["session_id"]]
        state = LongTaskState.verify(raw)
        self._validate_context_provenance(state)
        self.long_task_state = self.long_task.state = state
        self._context_commit_pending = False
        return deepcopy(receipt)

    def configure_long_task(self, original_request: str, *, hard_constraints=None) -> None:
        """Seed trusted host input once; later changes use structured batches."""
        constraints = list(hard_constraints or [])
        if (
            not isinstance(original_request, str)
            or not original_request.strip()
            or any(not isinstance(v, str) or not v.strip() for v in constraints)
        ):
            raise ValueError("context_configuration_invalid")
        request = {"original_request": original_request, "hard_constraints": constraints}
        checksum = digest(request)
        with self._mutex:
            self._context_owner()
            saved = self.store.latest("context_batch", "batch_id").get("initial-context")
            if saved:
                if checksum != saved["request_checksum"]:
                    raise ValueError("context_already_configured")
                self._replay_context(saved, checksum)
                return
            self._assert_context_current()
            if self.long_task_state.configuration_checksum:
                if checksum != self.long_task_state.configuration_checksum:
                    raise ValueError("context_already_configured")
                return
            self._context_quiescent()
            self.register_user_message("original-request", original_request)
            seed_id = "initial-structured-constraints"
            self.task_messages.register(
                seed_id, json.dumps(constraints, ensure_ascii=False), source_kind="runtime"
            )
            state = deepcopy(self.long_task_state)
            state.original_request = original_request
            state.original_request_ref = self.source_reference("original-request")
            state.configuration_checksum = checksum
            for statement in constraints:
                state.constraint_records.append(
                    new_constraint(
                        state.constraint_records,
                        action="add",
                        statement=statement,
                        source_kind="runtime",
                        source_refs=[self.source_reference(seed_id)],
                    )
                )
            state.hard_constraints = active_constraints(state.constraint_records)
            state.state_revision += 1
            state.context_revision += 1
            self._publish_context(
                state,
                {
                    "batch_id": "initial-context",
                    "request_checksum": checksum,
                    "records": [],
                    "expected_state_revision": self.long_task_state.state_revision,
                    "expected_plan_version": self.plan.plan_version if self.plan else 0,
                    "plan_id": self.plan.plan_id if self.plan else "",
                },
            )

    def submit_context_updates(
        self,
        batch_id: str,
        *,
        updates: list[dict],
        expected_state_revision: int,
        expected_plan_version: int,
    ) -> dict:
        """Validate every explicit update, then atomically publish one revision.

        Callers retain a stable batch_id across retries. Source identity comes
        from registered messages, not from fields in the candidate update.
        """
        if not isinstance(batch_id, str) or not batch_id or batch_id == "initial-context":
            raise ValueError("context_batch_id_invalid")
        if not isinstance(updates, list) or not updates:
            raise ValueError("context_updates_missing")
        updates = deepcopy(updates)
        request_checksum = digest(
            {
                "updates": updates,
                "expected_state_revision": expected_state_revision,
                "expected_plan_version": expected_plan_version,
            }
        )
        with self._mutex:
            self._context_owner()
            previous = self.store.latest("context_batch", "batch_id").get(batch_id)
            if previous:
                return self._replay_context(previous, request_checksum)
            self._assert_context_current()
            if not self.long_task_state.configuration_checksum:
                raise ValueError("context_not_configured")
            if type(expected_state_revision) is not int or (
                expected_state_revision != self.long_task_state.state_revision
            ):
                raise ValueError("context_state_revision_conflict")
            version = self.plan.plan_version if self.plan else 0
            if (
                type(expected_plan_version) is not int
                or expected_plan_version != version
                or (self.plan and not self.plan.verify())
            ):
                raise ValueError("context_plan_version_conflict")
            self._context_quiescent()
            state = deepcopy(self.long_task_state)
            records = []
            touched = set()
            for update in updates:
                record = self._context_update(state, update)
                key = record.get("constraint_id") or record["decision_id"]
                if key in touched:
                    raise ValueError("context_duplicate_target")
                touched.add(key)
                records.append(record)
            state.hard_constraints = active_constraints(state.constraint_records)
            state.state_revision += 1
            state.context_revision += 1
            self._assert_context_current()
            return self._publish_context(
                state,
                {
                    "batch_id": batch_id,
                    "request_checksum": request_checksum,
                    "records": records,
                    "expected_state_revision": expected_state_revision,
                    "expected_plan_version": expected_plan_version,
                    "plan_id": self.plan.plan_id if self.plan else "",
                },
            )

    def _context_update(self, state, update):
        common = {"kind", "action", "source_refs"}
        if not isinstance(update, dict) or not common <= set(update):
            raise ValueError("context_update_invalid")
        kind, action, refs = update["kind"], update["action"], update["source_refs"]
        if kind == "constraint":
            if set(update) - common - {"statement", "constraint_id", "expected_revision"}:
                raise ValueError("context_update_fields_invalid")
            self.task_messages.validate(refs, allowed={"user"})
            record = new_constraint(
                state.constraint_records, **{k: v for k, v in update.items() if k != "kind"}
            )
            state.constraint_records.append(record)
            return record
        if (
            kind != "decision"
            or action not in {"add", "replace", "revoke"}
            or set(update)
            - common
            - {
                "statement",
                "decision_id",
                "expected_revision",
                "node_id",
                "rationale",
                "evidence_refs",
            }
        ):
            raise ValueError("context_update_fields_invalid")
        kinds = self.task_messages.validate(refs, allowed={"user", "model"})
        if len(kinds) != 1:
            raise ValueError("decision_mixed_sources")
        source_kind = "user" if kinds == {"user"} else "technical"
        if self.plan is None:
            raise ValueError("decision_plan_missing")
        node_id = update.get("node_id")
        node = next((n for n in self.plan.nodes if n.node_id == node_id), None)
        if node is None or node.status in {"succeeded", "uncertain", "cancelled"}:
            raise ValueError("decision_node_unavailable")
        local = getattr(self.local, "attempt", None) or {}
        if node.status == "running" and local.get("node_id") != node_id:
            raise ValueError("decision_active_attempt_mismatch")
        key, revision = update.get("decision_id"), update.get("expected_revision")
        if (
            action == "add"
            and (key is not None or revision is not None)
            or (action != "add" and key is None)
        ):
            raise ValueError("decision_revision_conflict")
        if action == "revoke":
            prior = decision_history(state.key_decisions)[1].get(key)
            if (
                not prior
                or prior["node_id"] != node_id
                or (prior.get("source_kind", "technical") != source_kind)
            ):
                raise ValueError("decision_source_or_scope_mismatch")
            record = revoked_decision(
                state.key_decisions,
                self.plan,
                decision_id=key,
                expected_revision=revision,
                source_refs=refs,
            )
        else:
            record = new_decision(
                state.key_decisions,
                self.plan,
                self.evidence,
                update.get("statement"),
                rationale=update.get("rationale", ""),
                source=next(iter(kinds)),
                source_kind=source_kind,
                source_refs=refs,
                evidence_refs=update.get("evidence_refs", []),
                node_id=node_id,
                decision_id=key,
                expected_revision=revision,
            )
        state.key_decisions.append(record)
        return record
