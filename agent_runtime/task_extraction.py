"""Bounded natural-language candidates, durable progress and owner auto-commit."""

from __future__ import annotations

import json
from copy import deepcopy

from agent_runtime.budget_manager import BudgetManager
from agent_runtime.cancellation import CancelledError
from agent_runtime.errors import ContextBuildBlockedError
from agent_runtime.model_call_budget import complete_text
from agent_runtime.plan_runtime.constraint_records import constraint_history
from agent_runtime.plan_runtime.decisions import decision_history
from agent_runtime.plan_runtime.models import digest
from agent_runtime.task_extraction_schema import INSTRUCTIONS, SCHEMA_VERSION, parse_candidates
from agent_runtime.token_budget import TokenBudget

MAX_SEGMENTS = 16


class TaskExtraction:
    def __init__(self, agent, session, *, client=None):
        self.agent, self.session = agent, session
        self.client = client or getattr(agent, "light_client", None) or agent.model_client
        self.counter = TokenBudget(model=agent.config.model, provider=agent.config.provider)
        self.output_tokens = min(agent.config.max_new_tokens, 2048)
        self.input_limit = agent.config.input_window(self.output_tokens)
        if getattr(agent, "budget_manager", None) is None:
            agent.budget_manager = BudgetManager.from_config(agent.config)
        budgets = session.store.latest("extraction_budget", "budget_id")
        if budgets:
            agent.budget_manager.restore_missing_model_calls(
                next(iter(budgets.values()))["snapshot"], purpose="extraction"
            )

    def _save(self, record, **changes):
        self.session._context_owner()
        value = {**record, **deepcopy(changes)}
        self.session.store.append("task_extraction", value)
        return value

    def _block(self, record, reason, **details):
        self._save(record, phase="blocked", reason=reason, details=details)
        raise ContextBuildBlockedError(
            reason, metadata={"extraction_id": record["extraction_id"], **details}
        )

    def _budget_snapshot(self, snapshot):
        self.session._context_owner()
        self.session.store.append(
            "extraction_budget",
            {
                "budget_id": "run-extraction-budget",
                "snapshot": snapshot,
            },
        )

    def process_user_message(self, message_id, body, *, resolves=()):
        """Trusted owner intake. A clarification is a new source, not edited history."""
        return self._intake(message_id, body, "user", resolves=resolves)

    def process_model_message(self, message_id, body, *, node_id, evidence_refs):
        return self._intake(message_id, body, "model", node_id=node_id, evidence_refs=evidence_refs)

    def _pending(self, message, node_id, evidence_refs, resolves, records):
        additional, resolution_ids = [], []
        for original_id in resolves:
            original = next((r for r in records.values() if r["message_id"] == original_id), None)
            if (
                not original
                or original["phase"] != "blocked"
                or original["reason"] != "clarification_required"
                or message["source_kind"] != "user"
            ):
                raise ValueError("extraction_resolution_invalid")
            additional.append(self.session.task_messages.records()[original_id])
            additional.extend(original.get("additional_sources", []))
            resolution_ids.append(original["extraction_id"])
        return {
            "extraction_id": "extract-" + digest([SCHEMA_VERSION, message["checksum"]])[:24],
            "message_id": message["message_id"],
            "message_checksum": message["checksum"],
            "policy_version": SCHEMA_VERSION,
            "phase": "pending",
            "stage": "initial",
            "model_node_id": node_id,
            "evidence_refs": list(evidence_refs),
            "additional_sources": list({r["message_id"]: r for r in additional}.values()),
            "resolves": resolution_ids,
        }

    def _intake(self, message_id, body, source_kind, *, node_id="", evidence_refs=(), resolves=()):
        with self.session._mutex:
            session = self.session
            session._context_owner()
            message = session.task_messages.prepare(message_id, body, source_kind=source_kind)
            records = session.store.latest("task_extraction", "extraction_id")
            key = "extract-" + digest([SCHEMA_VERSION, message["checksum"]])[:24]
            if key not in records:
                pending = self._pending(message, node_id, evidence_refs, resolves, records)
                entries = []
                if message_id not in session.task_messages.records():
                    entries.append(("task_message", message))
                entries.append(("task_extraction", pending))
                session._context_owner()
                session.store.append_many(entries)
                session.cut("extraction_intake_saved")
            return self.process_registered(message_id, node_id=node_id, evidence_refs=evidence_refs)

    def _snapshot(self, node_id, evidence_refs):
        session = self.session
        state = session.long_task_state
        plan = session.plan
        _, constraints = constraint_history(state.constraint_records)
        _, decisions = decision_history(state.key_decisions)
        nodes = (
            [n for n in plan.nodes if n.status not in {"succeeded", "uncertain", "cancelled"}]
            if plan
            else []
        )
        refs = list(dict.fromkeys(evidence_refs))
        if len(refs) > 16 or any(not session.evidence.valid(ref) for ref in refs):
            raise ValueError("extraction_evidence_unusable")
        raw = state.to_dict()
        raw["state_id"] = session.identity["session_id"]
        return {
            "state_revision": state.state_revision,
            "state_checksum": digest(raw),
            "plan_version": plan.plan_version if plan else 0,
            "plan_checksum": plan.plan_checksum if plan else "",
            "constraints": list(constraints.values()),
            "decisions": list(decisions.values()),
            "nodes": [
                {"node_id": n.node_id, "kind": n.kind, "objective": n.objective} for n in nodes
            ],
            "model_node_id": node_id,
            "evidence_refs": refs,
            "evidence": [
                {
                    "evidence_ref": ref,
                    "checksum": session.evidence.inspect(ref)["record_checksum"],
                    "kind": session.evidence.get(ref)["kind"],
                }
                for ref in refs
            ],
        }

    def _check_snapshot(self, snapshot):
        session = self.session
        session._assert_context_current()
        raw = session.long_task_state.to_dict()
        raw["state_id"] = session.identity["session_id"]
        if snapshot["state_checksum"] != digest(raw) or snapshot["plan_checksum"] != (
            session.plan.plan_checksum if session.plan else ""
        ):
            raise ValueError("extraction_snapshot_stale")
        if any(
            session.evidence.inspect(item["evidence_ref"]).get("record_checksum")
            != item["checksum"]
            or not session.evidence.valid(item["evidence_ref"])
            for item in snapshot["evidence"]
        ):
            raise ValueError("extraction_evidence_changed")

    def _prompt(self, message, snapshot, segment, *, previous=(), additional=(), binding=False):
        data = {
            "schema_version": SCHEMA_VERSION,
            "source": {k: message[k] for k in ("message_id", "source_kind", "checksum")},
            "message_checksum": message["checksum"],
            "segment": {**segment, "body": message["body"][segment["start"] : segment["end"]]},
            "state": snapshot,
            "previous_segment_candidates": list(previous),
            "clarification_sources": list(additional),
            "binding_deferred_choices": binding,
        }
        return (
            INSTRUCTIONS + "\nINPUT_JSON:\n" + json.dumps(data, ensure_ascii=False, sort_keys=True)
        )

    def _segments(self, message, snapshot, additional, binding):
        body, start, segments = message["body"], 0, []
        while start < len(body):
            low, high, fit = start + 1, len(body), start
            while low <= high:
                end = (low + high) // 2
                segment = {"message_id": message["message_id"], "start": start, "end": end}
                prompt = self._prompt(
                    message, snapshot, segment, additional=additional, binding=binding
                )
                # Reserve room for previous candidates and the one format repair.
                if self.counter.count(prompt) <= max(0, self.input_limit - 800):
                    fit, low = end, end + 1
                else:
                    high = end - 1
            if fit == start or len(segments) >= MAX_SEGMENTS:
                raise ValueError("extraction_input_over_budget")
            end = fit
            if fit < len(body):
                boundary = max(body.rfind(c, start, fit) for c in ("\n", "。", ".", ";", "；"))
                if boundary >= start + (fit - start) // 2:
                    end = boundary + 1
            segments.append({"message_id": message["message_id"], "start": start, "end": end})
            start = end
        return segments

    def process_registered(self, message_id, *, node_id="", evidence_refs=(), resolves=()):
        session = self.session
        with session._mutex:
            session._context_owner()
            message = session.task_messages.records().get(message_id)
            if message is None or message["source_kind"] not in {"user", "model"}:
                raise ValueError("extraction_source_not_allowed")
            key = "extract-" + digest([SCHEMA_VERSION, message["checksum"]])[:24]
            records = session.store.latest("task_extraction", "extraction_id")
            record = records.get(key)
            if record and record["phase"] in {"committed", "resolved"}:
                self._resolve_records(record)
                return deepcopy(record)
            if record and record["phase"] == "blocked":
                raise ContextBuildBlockedError(record["reason"], metadata=record.get("details", {}))
            if record is None:
                record = self._save(
                    self._pending(message, node_id, evidence_refs, resolves, records)
                )
                session.cut("extraction_enrolled")
            if record["model_node_id"] != node_id and message["source_kind"] == "model":
                raise ValueError("extraction_model_scope_conflict")
            if record["phase"] == "awaiting_node":
                if session.plan is None:
                    return deepcopy(record)
                record = self._save(
                    record,
                    phase="pending",
                    stage="binding",
                    snapshot=None,
                    initial_updates=record["updates"],
                    choices=record["deferred"],
                    segments=None,
                    updates=None,
                    deferred=None,
                )
            if record["phase"] == "ready":
                return self._commit(record)
            session._assert_context_current()
            session._context_quiescent()
            if not record.get("snapshot"):
                try:
                    snapshot = self._snapshot(record["model_node_id"], record["evidence_refs"])
                    segments = self._segments(
                        message,
                        snapshot,
                        record["additional_sources"],
                        record["stage"] == "binding",
                    )
                except ValueError as exc:
                    return self._block(record, str(exc))
                record = self._save(
                    record, phase="extracting", snapshot=snapshot, segments=segments
                )
            candidates, deferred, clarifications = [], [], []
            for index, segment in enumerate(record["segments"]):
                try:
                    self._check_snapshot(record["snapshot"])
                    result = self._extract_segment(record, message, index, segment, candidates)
                    if record["stage"] == "binding" and any(
                        item["kind"] != "decision" for item in result["updates"]
                    ):
                        raise ValueError("extraction_binding_choices_only")
                except ContextBuildBlockedError:
                    raise
                except CancelledError:
                    raise
                except Exception as exc:
                    return self._block(record, "extraction_failed", detail=str(exc))
                candidates.extend(result["updates"])
                deferred.extend(result["deferred"])
                clarifications.extend(result["clarifications"])
            if clarifications:
                return self._block(
                    record,
                    "clarification_required",
                    questions=clarifications,
                    candidates=candidates,
                    deferred=deferred,
                )
            if record["stage"] == "binding":
                expected = sorted(
                    digest([c["statement"], c["source_refs"]]) for c in record["choices"]
                )
                actual = sorted(digest([c.get("statement"), c["source_refs"]]) for c in candidates)
                if actual != expected or any(c["action"] != "add" for c in candidates):
                    return self._block(
                        record,
                        "clarification_required",
                        questions=[
                            {"question": "待绑定决策未完整绑定，请明确每项选择适用的节点。"}
                        ],
                    )
            if record["resolves"] and not candidates and not deferred:
                return self._block(
                    record,
                    "clarification_required",
                    questions=[{"question": "请明确重述澄清后的最终约束或选择。"}],
                )
            # Exact duplicate statements have no new state effect. Meaning is not compared.
            unique, targets = [], set()
            for candidate in candidates:
                if candidate["action"] == "add" and any(
                    old["statement"] == candidate["statement"]
                    and old["status"] == "active"
                    and (candidate["kind"] != "decision" or old["node_id"] == candidate["node_id"])
                    for old in record["snapshot"][
                        "constraints" if candidate["kind"] == "constraint" else "decisions"
                    ]
                ):
                    continue
                target = (
                    candidate["kind"],
                    candidate.get("constraint_id")
                    or candidate.get("decision_id")
                    or (candidate.get("node_id", ""), candidate.get("statement")),
                )
                if target in targets:
                    if candidate not in unique:
                        return self._block(
                            record,
                            "clarification_required",
                            questions=[{"question": "同一批次的目标存在重复或冲突，请明确选择。"}],
                        )
                    continue
                targets.add(target)
                unique.append(candidate)
            record = self._save(record, phase="ready", updates=unique, deferred=deferred)
            session.cut("extraction_result_saved")
            return self._commit(record)

    def _extract_segment(self, record, message, index, segment, previous):
        session = self.session
        part_id = record["extraction_id"] + ":" + record["stage"] + ":" + str(index)
        saved = session.store.latest("extraction_segment", "segment_id").get(part_id)
        if saved and saved.get("result") is not None:
            return saved["result"]
        segments = session.store.latest("extraction_segment", "segment_id").values()
        repair_used = any(
            p.get("repair_used")
            for p in segments
            if p.get("extraction_id") == record["extraction_id"]
        )
        attempt = saved.get("attempt", 0) if saved else 0
        prompt = self._prompt(
            message,
            record["snapshot"],
            segment,
            previous=previous,
            additional=record["additional_sources"],
            binding=record["stage"] == "binding",
        )
        if record["stage"] == "binding":
            choices = [
                c
                for c in record["choices"]
                if any(
                    ref["message_id"] != segment["message_id"]
                    or segment["start"] <= ref["start"] < ref["end"] <= segment["end"]
                    for ref in c["source_refs"]
                )
            ]
            prompt += (
                "\nBind ONLY these deferred choices; do not reapply constraints or old updates:\n"
            )
            prompt += json.dumps(choices, ensure_ascii=False)
        if saved:
            prompt += (
                "\nPrevious output was invalid or interrupted. Return the complete valid JSON."
            )
        while True:
            if self.counter.count(prompt) > self.input_limit:
                raise ValueError("extraction_input_over_budget")
            if attempt and repair_used:
                raise ValueError("extraction_repair_exhausted")
            repair = attempt > 0
            repair_used |= repair
            attempt += 1
            part = {
                "segment_id": part_id,
                "extraction_id": record["extraction_id"],
                "attempt": attempt,
                "repair_used": repair_used,
                "segment": segment,
                "phase": "started",
            }
            session.store.append("extraction_segment", part)
            session.cut("extraction_call_started")
            try:
                raw = complete_text(
                    self.agent,
                    self.client,
                    prompt,
                    max_output_tokens=self.output_tokens,
                    purpose="extraction",
                    timeout_s=30,
                    on_reserved=self._budget_snapshot,
                )
            finally:
                self._budget_snapshot(self.agent.budget_manager.snapshot())
            try:
                result = parse_candidates(
                    raw,
                    session=session,
                    snapshot=record["snapshot"],
                    messages=[message, *record["additional_sources"]],
                    segment=segment,
                    allow_deferred=session.plan is None,
                )
            except (ValueError, KeyError, TypeError) as exc:
                session.store.append(
                    "extraction_segment", {**part, "phase": "invalid", "error": str(exc)}
                )
                if repair_used:
                    raise ValueError("extraction_invalid_output") from exc
                prompt += "\nREPAIR: " + str(exc)[:160] + ". Return only the full schema JSON."
                continue
            session.store.append(
                "extraction_segment", {**part, "phase": "result", "result": result}
            )
            session.cut("extraction_segment_saved")
            return result

    def _commit(self, record):
        session = self.session
        snapshot = record["snapshot"]
        batch_id = record["extraction_id"] + ":" + record["stage"]
        batches = session.store.latest("context_batch", "batch_id")
        if batch_id not in batches:
            try:
                session._context_owner()
                session._context_quiescent()
                self._check_snapshot(snapshot)
            except ValueError as exc:
                return self._block(record, str(exc))
        if record["updates"]:
            try:
                session.submit_context_updates(
                    batch_id,
                    updates=record["updates"],
                    expected_state_revision=snapshot["state_revision"],
                    expected_plan_version=snapshot["plan_version"],
                )
            except ValueError as exc:
                return self._block(record, "extraction_commit_rejected", detail=str(exc))
        session.cut("extraction_batch_committed")
        result = self._save(record, phase="awaiting_node" if record["deferred"] else "committed")
        self._resolve_records(result)
        return result

    def _resolve_records(self, record):
        # A validated deferred answer settles the old ambiguity, while its own
        # awaiting_node record still fences tools until Plan binding completes.
        if record["phase"] in {"committed", "awaiting_node", "resolved"}:
            for original_id in record["resolves"]:
                original = self.session.store.latest("task_extraction", "extraction_id")[
                    original_id
                ]
                if original["phase"] != "resolved":
                    original = self._save(
                        original, phase="resolved", resolved_by=record["extraction_id"]
                    )
                self._resolve_records(original)

    def resume_pending(self):
        results = []
        records = self.session.store.latest("task_extraction", "extraction_id")
        for record in records.values():
            self._resolve_records(record)
        # Resume a saved answer before consulting the older blocked question.
        pending = self.session.store.latest("task_extraction", "extraction_id").values()
        for record in sorted(
            pending, key=lambda r: (r["phase"] == "blocked", not bool(r["resolves"]))
        ):
            if record["phase"] not in {"committed", "resolved"}:
                results.append(
                    self.process_registered(
                        record["message_id"],
                        node_id=record["model_node_id"],
                        evidence_refs=record["evidence_refs"],
                    )
                )
        return results

    def before_model(self):
        self.resume_pending()
        self.session.require_extraction_resolved()

    def before_tools(self, text):
        """Only visible owner prose before a confirmed effect can become a choice."""
        if not isinstance(text, str) or not text.strip():
            return
        session = self.session
        attempt = getattr(session.local, "attempt", None) or {}
        if not attempt or any(
            op.get("changed_paths") for op in session.operations(attempt["attempt_id"])
        ):
            return
        context = session.build_required_context(attempt["node_id"])
        if context["evidence_phase"] != "before_effect":
            return
        message_id = "model-choice-" + digest([attempt["attempt_id"], text])[:24]
        self.process_model_message(
            message_id, text, node_id=attempt["node_id"], evidence_refs=context["evidence_refs"]
        )
        session.require_extraction_resolved()
