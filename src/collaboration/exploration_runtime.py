"""Two isolated read workers, owner collection, cancellation and safe recovery."""

from __future__ import annotations

import copy
import hashlib
import os
import threading
import time
from dataclasses import asdict

from agent_runtime.budget_manager import BudgetManager
from agent_runtime.cancellation import CancellationToken
from agent_runtime.context_runtime import ObservationStore
from agent_runtime.plan_runtime.models import digest, new_id
from agent_runtime.plan_runtime.processes import confirmed_exited, process_identity
from agent_runtime.plan_runtime.workspace import snapshot, workspace_id
from agent_runtime.read_permits import read_permits
from agent_runtime.run_coordination.models import ResourceResult
from agent_runtime.turn_progress import replay_progress
from src.agents.explorer import ExplorerToken, create_explorer_agent
from src.collaboration.contracts import AgentTask, TaskStatus
from src.collaboration.exploration_contracts import (
    ACTIVE,
    ExplorationLimits,
    in_scope,
    validate_requests,
)
from src.collaboration.exploration_results import merge_findings
from src.collaboration.exploration_store import ExplorationStore
from src.collaboration.store import LeaseConflictError


class ExplorationRuntime:
    def __init__(
        self,
        agent,
        *,
        run_id,
        parent_task_id,
        plan_session=None,
        coordinator=None,
        client_factory=None,
        limits=None,
        store=None,
        event_sink=None,
    ):
        self.agent, self.root = agent, agent.tool_context.root
        self.run_id, self.parent_task_id = run_id, parent_task_id
        self.plan_session, self.coordinator = plan_session, coordinator
        self.limits = limits or ExplorationLimits()
        self.store = store or ExplorationStore(self.root, state_root=agent.state_root)
        self.budget = getattr(agent, "_run_budget_manager", None) or BudgetManager.from_config(
            agent.config
        )
        agent._run_budget_manager = self.budget
        self.event_sink = event_sink
        self.store.exploration_event_sink = event_sink
        self.client_factory = client_factory or self._client
        self.parent_token = getattr(agent, "cancel_token", None) or CancellationToken()
        self.owner_thread = threading.get_ident()
        self.workers, self.tokens = {}, {}
        self.lock = threading.RLock()
        self.closed = False
        self._unsubscribe = self.parent_token.add_callback(self._request_cancel)
        if coordinator is not None:
            coordinator.adapters["exploration_task"] = self
        self._monitor_stop = threading.Event()
        self._monitor = threading.Thread(target=self._watch, daemon=True)
        self._monitor.start()

    def _client(self, task):
        # Provider configuration may be shared; usage and mutable call state may not.
        client = copy.copy(self.agent.model_client)
        for name, value in vars(client).items():
            if isinstance(value, dict | list | set):
                setattr(client, name, copy.deepcopy(value))
        if hasattr(client, "_init_usage_tracking"):
            client._init_usage_tracking()
        if hasattr(client, "timeout"):
            client.timeout = min(client.timeout, self.limits.deadline_s)
        return client

    def _owner(self):
        if threading.get_ident() != self.owner_thread:
            raise ValueError("exploration_owner_thread_required")
        if self.closed:
            raise ValueError("exploration_runtime_closed")
        self.parent_token.check()
        if self.coordinator:
            self.coordinator.assert_can_dispatch()

    def _plan(self):
        plan = self.plan_session.plan if self.plan_session else None
        node = (
            (getattr(self.plan_session.local, "attempt", None) or {}).get("node_id", "")
            if self.plan_session
            else ""
        )
        return {
            "plan_id": plan.plan_id if plan else "",
            "plan_version": plan.plan_version if plan else 0,
            "node_id": node,
        }

    def tasks(self):
        return [
            t
            for t in self.store.list_tasks(self.run_id)
            if t.parent_task_id == self.parent_task_id and "exploration" in t.payload
        ]

    def _input(self, oid, scopes, versions):
        # Only the owner's session or verified Plan evidence may supply input.
        session = self.plan_session
        record = (
            next(
                (
                    r
                    for r in session.store.latest("evidence", "evidence_id").values()
                    if r.get("observation_id") == oid and session.evidence.valid(r["evidence_id"])
                ),
                None,
            )
            if session
            else None
        )
        if record:
            obs = record["observation_record"]
            text = session.store.get_blob(record["blob_ref"])
            files = record["file_versions"]
        else:
            observations = ObservationStore(self.agent.session, self.root, self.agent.state_root)
            try:
                item = observations.get(oid)
                if (
                    item is None
                    or item.run_id != self.run_id
                    or item.session_id != observations.session_id
                ):
                    raise ValueError("exploration_input_scope_mismatch")
                obs, text = asdict(item), observations.expand(oid)
                files = item.provenance.get("file_versions", {})
            finally:
                observations.close()
        if (
            obs.get("stale")
            or not files
            or not text
            or hashlib.sha256(text.encode("utf-8", "replace")).hexdigest() != obs["checksum"]
            or any(versions.get(p) != h or not in_scope(p, scopes) for p, h in files.items())
        ):
            raise ValueError("exploration_input_stale_or_outside_scope")
        return {"observation_id": oid, "summary": obs["summary"][:500], "checksum": obs["checksum"]}

    def delegate(self, requests):
        self._owner()
        self.sync_budget()
        tasks = []
        try:
            validated = validate_requests(self.root, requests)
            versions, plan = snapshot(self.root), self._plan()
            batch = new_id("exploration-batch")
            tasks = []
            for request in validated:
                inputs = [
                    self._input(oid, request["scope_paths"], versions)
                    for oid in request["input_observation_ids"]
                ]
                task = AgentTask(
                    run_id=self.run_id,
                    parent_task_id=self.parent_task_id,
                    role="explorer",
                    kind=request["kind"],
                    phase="explore",
                    status=TaskStatus.READY,
                    deadline_at=time.time() + self.limits.deadline_s,
                    budget={"tokens": self.limits.tokens, "tools": self.limits.tool_calls},
                    payload={
                        "exploration": {
                            "schema_version": "1",
                            "batch_id": batch,
                            "turn_id": str(
                                getattr(
                                    getattr(self.agent, "_turn_event_emitter", None), "turn_id", ""
                                )
                                or f"exploration-{self.run_id}-"
                                f"{self.agent.session.get('_turn_counter', 0)}"
                            ),
                            "workspace_id": workspace_id(self.root),
                            "workspace_revision": versions,
                            **plan,
                            **request,
                            "input_summaries": inputs,
                            "max_model_turns": self.limits.model_turns,
                            "max_tool_calls": self.limits.tool_calls,
                            "token_reservation": self.limits.tokens,
                            "status": "queued",
                            "lease_generation": 0,
                            "charged_tokens": self.limits.tokens,
                        }
                    },
                )
                tasks.append(task)
            budget_key = "exploration:" + batch
            costs = {
                "prompt_tokens": len(tasks) * self.limits.tokens,
                "llm_calls": len(tasks) * self.limits.model_turns,
                "tool_calls": len(tasks) * self.limits.tool_calls,
            }
            if not self.budget.reserve_scope(budget_key, costs):
                raise ValueError("exploration_global_budget_exhausted")
            try:
                self.store.submit_batch(tasks, run_token_limit=self.limits.run_tokens)
            except BaseException:
                self.budget.reconcile_scope(budget_key, {})
                raise
            self._emit(tasks[0], "exploration_batch_submitted")
            for task in tasks:
                try:
                    self._start(task)
                except Exception:
                    self.store.invalidate_worker(task.task_id, reason="failed", stopped=True)
            return [
                {
                    "handle": t.task_id,
                    "task_id": t.task_id,
                    "status": "queued",
                    "parent_task_id": t.parent_task_id,
                    "turn_id": t.payload["exploration"]["turn_id"],
                    **plan,
                }
                for t in tasks
            ]
        except Exception:
            # Reject before any records are created for invalid requests.
            if tasks:
                if not self.store.get_task(tasks[0].task_id):
                    self._emit(tasks[0], "exploration_batch_rejected")
            else:
                rejected = AgentTask(
                    run_id=self.run_id,
                    parent_task_id=self.parent_task_id,
                    role="explorer",
                    kind="rejected",
                    payload={
                        "exploration": {
                            "turn_id": f"exploration-{self.run_id}",
                            "status": "rejected",
                            "lease_generation": 0,
                            "workspace_id": workspace_id(self.root),
                            **self._plan(),
                        }
                    },
                )
                self._emit(rejected, "exploration_batch_rejected")
            raise

    def _emit(self, task, event, **fields):
        self.store.emit(task, event, **fields)

    def _start(self, task):
        with self.lock:
            existing = self.workers.get(task.task_id)
            if existing is not None and existing.is_alive():
                return
            if self.closed or self.parent_token.is_cancelled:
                self.store.invalidate_worker(task.task_id, stopped=True)
                return
            if self.coordinator:
                resource_id = "exploration:" + task.task_id
                if not any(
                    r.resource_id == resource_id
                    for r in self.coordinator.store.resources(self.run_id)
                ):
                    self.coordinator.register_resource(
                        resource_id=resource_id,
                        kind="exploration_task",
                        effect="read",
                        payload={"task_id": task.task_id},
                    )
            token = ExplorerToken(self.parent_token, task.deadline_at)
            self.tokens[task.task_id] = token
            worker = threading.Thread(
                target=self._run,
                args=(task.task_id, token),
                daemon=True,
                name=f"fixloop-explorer-{task.task_id}",
            )
            self.workers[task.task_id] = worker
            worker.start()

    def _run(self, task_id, token):
        claimed = None
        try:
            with read_permits(self.root, self.run_id).lease(token):
                token.check()
                claimed = self.store.claim(task_id, process_identity(os.getpid()) or {})
                data = claimed.payload["exploration"]
                projection = {
                    "kind": claimed.kind,
                    "question": data["question"],
                    "scope_paths": data["scope_paths"],
                    "plan": {k: data[k] for k in ("plan_id", "plan_version", "node_id")},
                    "inputs": data["input_summaries"],
                }
                child = create_explorer_agent(
                    self.client_factory(claimed),
                    root=self.root,
                    scopes=data["scope_paths"],
                    token=token,
                    limits=self.limits,
                    state_root=self.agent.state_root,
                )
                child.shared_run_id = self.run_id
                result = child.explore(
                    claimed,
                    projection,
                    self.limits,
                    lambda event, **fields: self._emit(claimed, event, **fields),
                )
                result["checksum"] = digest(result)
                self.store.finish(claimed, result)
        except LeaseConflictError:
            pass  # An invalidated generation cannot publish a late result.
        except Exception:
            if claimed:
                result = {
                    "status": "failed",
                    "usage": {"tokens": None},
                    "findings": [],
                    "unknowns": ["Worker failed before a confirmed result."],
                    "error_code": "exploration_worker_failed",
                }
                try:
                    self.store.finish(claimed, result)
                except LeaseConflictError:
                    pass
            else:
                self.store.invalidate_worker(task_id, reason="failed", stopped=True)
        finally:
            if claimed:
                self.store.confirm_cleanup(task_id, claimed.payload["exploration"]["attempt_id"])

    def _handles(self, handles):
        if (
            not isinstance(handles, list)
            or not 1 <= len(handles) <= 2
            or any(not isinstance(h, str) for h in handles)
            or len(set(handles)) != len(handles)
        ):
            raise ValueError("invalid_exploration_handles")
        tasks = [self.store.get_task(h) for h in handles]
        if any(
            t is None
            or t.run_id != self.run_id
            or t.parent_task_id != self.parent_task_id
            or t.role != "explorer"
            or "exploration" not in t.payload
            for t in tasks
        ):
            raise ValueError("exploration_handle_scope_mismatch")
        return tasks

    def collect(self, handles, wait_ms=0):
        self._owner()
        if type(wait_ms) is not int or not 0 <= wait_ms <= self.limits.max_wait_ms:
            raise ValueError("invalid_exploration_wait")
        self._handles(handles)  # Entire handle list is validated before waiting or collection.
        deadline = time.monotonic() + wait_ms / 1000
        while time.monotonic() < deadline:
            self.parent_token.check()
            if all(
                t.payload["exploration"]["status"] not in ACTIVE for t in self._handles(handles)
            ):
                break
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        results, versions = [], snapshot(self.root)
        for task in self._handles(handles):
            data = task.payload["exploration"]
            result = copy.deepcopy(data.get("result") or {"findings": [], "unknowns": []})
            result.update(
                task_id=task.task_id,
                status=data["status"],
                result_ref=data.get("result_ref", ""),
                cleanup_confirmed=data.get("cleanup_confirmed", False),
            )
            if data["status"] == "completed" and not self._fresh(task, result, versions):
                result["status"] = "stale"
                self._once(task, "subagent_evidence_stale")
            self._once(task, "subagent_result_collected", only_terminal=True)
            results.append(result)
        findings = merge_findings(results)
        self.sync_budget()
        if any(f["review"] == "needs_review" for f in findings):
            for task in self._handles(handles):
                self._once(task, "subagent_review_required")
        return {"tasks": results, "findings": findings, "review_required": bool(findings)}

    def _fresh(self, task, result, versions):
        data = task.payload["exploration"]
        stored = data.get("result") or {}
        if stored.get("checksum") != digest({k: v for k, v in stored.items() if k != "checksum"}):
            return False
        if (
            result.get("task_id") != task.task_id
            or result.get("parent_task_id") != self.parent_task_id
            or result.get("run_id") != self.run_id
            or result.get("workspace_id") != workspace_id(self.root)
            or result.get("attempt_id") != data.get("attempt_id")
            or result.get("lease_generation") != data["lease_generation"]
            or not result.get("complete")
            or data["workspace_revision"] != versions
            or any(data[k] != self._plan()[k] for k in ("plan_id", "plan_version"))
        ):
            return False
        observations = self._observations(data)
        try:
            for source in result.get("observations", []):
                observation = observations.get(source["observation_id"])
                if not self._valid_observation(observation, source, observations, data, versions):
                    return False
        finally:
            observations.close()
        return True

    def _observations(self, data):
        state = {
            "id": data["attempt_id"],
            "run_id": self.run_id,
            "session_scope": {
                "workspace_id": data["workspace_id"],
                "session_id": data["attempt_id"],
            },
        }
        return ObservationStore(state, self.root, self.agent.state_root)

    def _valid_observation(self, observation, source, store, data, versions):
        return bool(
            observation
            and not observation.stale
            and observation.lifecycle == "active"
            and observation.run_id == self.run_id
            and observation.workspace_id == data["workspace_id"]
            and observation.session_id == data["attempt_id"]
            and source.get("complete")
            and observation.checksum == source["checksum"]
            and hashlib.sha256(
                store.expand(observation.observation_id).encode("utf-8", "replace")
            ).hexdigest()
            == source["checksum"]
            and all(versions.get(p) == h for p, h in source["file_versions"].items())
        )

    def _once(self, task, event, only_terminal=False):
        def collect(current, conn):
            data = current.payload["exploration"]
            if only_terminal and data["status"] in ACTIVE:
                return False
            key = f"{event}:{data.get('attempt_id', '')}"
            if key in data.setdefault("emitted", []):
                return False
            data["emitted"].append(key)
            if event == "subagent_result_collected" and self.plan_session:
                data["owner_prior_operations"] = list(
                    self.plan_session.store.latest("operation", "operation_id")
                )

        self.store.mutate(task.task_id, collect, event)

    def _request_cancel(self):
        with self.lock:
            for task in self.tasks():
                if task.payload["exploration"]["status"] in ACTIVE:
                    self.store.invalidate_worker(task.task_id)
                    if task.task_id in self.tokens:
                        self.tokens[task.task_id].cancel("parent_cancelled")

    def _watch(self):
        while not self._monitor_stop.wait(0.05):
            for task in self.tasks():
                data = task.payload["exploration"]
                if data["status"] in {"queued", "running"} and time.time() >= task.deadline_at:
                    token = self.tokens.get(task.task_id)
                    if token:
                        token.cancel("deadline")
                    # An overdue synchronous provider/tool remains leased until it exits.
                    self.store.invalidate_worker(task.task_id, reason="timed_out")

    def drain(self, *, cancel=False):
        if cancel:
            self._request_cancel()
        deadline = time.monotonic() + self.limits.cleanup_s
        for worker in list(self.workers.values()):
            worker.join(max(0, deadline - time.monotonic()))
        if any(w.is_alive() for w in self.workers.values()) or any(
            t.payload["exploration"]["status"] in ACTIVE for t in self.tasks()
        ):
            raise ValueError("exploration_cleanup_unconfirmed")

    def before_write(self):
        self._owner()
        self.drain(cancel=True)
        self.record_owner_review()

    def record_owner_review(self):
        """Only owner rereads create authoritative Plan evidence; claims stay candidates."""
        self._owner()
        session = self.plan_session
        if session is None:
            return []
        reviewed = session.store.latest("exploration_review", "review_id")
        refs = []
        for task in self.tasks():
            data = task.payload["exploration"]
            if not any(k.startswith("subagent_result_collected:") for k in data.get("emitted", [])):
                continue
            result = data.get("result") or {}
            if data["status"] != "completed" or not self._fresh(task, result, snapshot(self.root)):
                continue
            for finding in result["findings"]:
                key = digest([task.task_id, data["attempt_id"], finding])
                if key in reviewed:
                    continue
                selected = []
                for op in session.store.latest("operation", "operation_id").values():
                    if op["operation_id"] in data.get("owner_prior_operations", []):
                        continue
                    args = op.get("arguments", {})
                    # Plan operations store the actual arguments with their evidence record.
                    for ref in op.get("evidence_refs", []):
                        fact = session.evidence.get(ref) or {}
                        args = fact.get("tool_arguments", args)
                        range_ = finding.get("range") or {}
                        if (
                            op["tool"] == "read_file"
                            and op["status"] == "success"
                            and op.get("execution_stopped")
                            and op.get("phase") == "result_recorded"
                            and args.get("path") == finding["path"]
                            and int(args.get("start", 1)) <= range_.get("start_line", 1)
                            and int(args.get("end", 100)) + 1 >= range_.get("end_line", 1)
                            and fact.get("file_versions", {}).get(finding["path"])
                            == finding["file_hash"]
                            and session.evidence.valid(ref)
                        ):
                            selected.append(ref)
                if selected:
                    session.store.append(
                        "exploration_review",
                        {
                            "review_id": key,
                            "task_id": task.task_id,
                            "attempt_id": data["attempt_id"],
                            "candidate": finding,
                            "owner_evidence_refs": selected,
                            "decision": "owner_reinspected_source; statement remains a candidate",
                        },
                    )
                    session.long_task.record_decision(
                        "Owner reinspected exploration source before editing: " + finding["path"],
                        source=key,
                    )
                    refs.extend(selected)
        if refs:
            session.long_task.add_evidence(list(dict.fromkeys(refs)))
            session._persist_long_task_state()
        return refs

    def checkpoint(self):
        self.sync_budget()
        tasks = [
            {
                "task_id": t.task_id,
                "attempt_id": t.payload["exploration"].get("attempt_id", ""),
                "lease_generation": t.payload["exploration"]["lease_generation"],
                "result_ref": t.payload["exploration"].get("result_ref", ""),
                "charged_tokens": t.payload["exploration"]["charged_tokens"],
                "receipts": t.payload["exploration"].get("receipts", []),
            }
            for t in self.tasks()
        ]
        raw = {
            "run_id": self.run_id,
            "parent_task_id": self.parent_task_id,
            "tasks": tasks,
            "plan": self._plan(),
            "event_seq": len(self.store.progress_events(self.run_id)),
            "global_budget": self.budget.snapshot(),
        }
        return {**raw, "checksum": digest(raw)}

    def restore(self, seal=None):
        self._owner()
        if seal:
            raw = {k: v for k, v in seal.items() if k != "checksum"}
            if (
                seal.get("checksum") != digest(raw)
                or seal.get("run_id") != self.run_id
                or seal.get("parent_task_id") != self.parent_task_id
            ):
                raise ValueError("exploration_checkpoint_invalid")
            for item in seal["tasks"]:
                self._handles([item["task_id"]])
            if not any(self.budget.snapshot()["used"].values()):
                self.budget.restore(seal.get("global_budget"))
        for task in self.tasks():
            data = task.payload["exploration"]
            if data["status"] in {"running", "worker_lost"}:
                key = new_id("exploration-retry")
                if not self.budget.reserve_scope(
                    key,
                    {
                        "prompt_tokens": task.budget["tokens"],
                        "llm_calls": self.limits.model_turns,
                        "tool_calls": self.limits.tool_calls,
                    },
                ):
                    raise ValueError("exploration_global_budget_exhausted")
                try:
                    task = self.store.retry(
                        task.task_id,
                        versions=snapshot(self.root),
                        deadline_s=self.limits.deadline_s,
                        run_token_limit=self.limits.run_tokens,
                        stopped=lambda t: t.payload["exploration"].get("cleanup_confirmed")
                        or confirmed_exited(t.payload["exploration"].get("owner", {})),
                    )
                finally:
                    self.budget.reconcile_scope(key, {})
                self.sync_budget()
            if task.payload["exploration"]["status"] == "queued":
                self._start(task)
        return self.checkpoint()

    def sync_budget(self):
        batches = {}
        for task in self.tasks():
            data = task.payload["exploration"]
            costs = batches.setdefault(
                "exploration:" + data["batch_id"],
                {"prompt_tokens": 0, "llm_calls": 0, "tool_calls": 0},
            )
            costs["prompt_tokens"] += data["charged_tokens"]
            receipts = data.get("receipts", [])
            for receipt in receipts:
                costs["llm_calls"] += receipt.get("model_turns", self.limits.model_turns)
                costs["tool_calls"] += receipt.get("tool_calls", self.limits.tool_calls)
            if not data.get("reservation_settled"):
                costs["llm_calls"] += self.limits.model_turns
                costs["tool_calls"] += self.limits.tool_calls
        for key, costs in batches.items():
            self.budget.reconcile_scope(key, costs)
        self.agent.session["runtime_budget"] = self.budget.snapshot()

    def progress(self):
        return replay_progress(self.store.progress_events(self.run_id))

    def reconcile(self, resource):
        task = self.store.get_task(resource.payload["task_id"])
        if task is None:
            return ResourceResult(resource.resource_id, "not_started", cleanup="confirmed")
        data = task.payload["exploration"]
        stopped = (
            data.get("cleanup_confirmed")
            or (data["status"] == "queued")
            or confirmed_exited(data.get("owner", {}))
        )
        return ResourceResult(
            resource.resource_id,
            "completed" if stopped else "unknown",
            cleanup="confirmed" if stopped else "unknown",
        )

    def cancel(self, resource, request_id):
        self._request_cancel()
        try:
            self.drain()
        except ValueError:
            pass
        return self.reconcile(resource)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._unsubscribe()
        self._monitor_stop.set()
        self._monitor.join(1)
        try:
            self.drain(cancel=True)
        finally:
            self.store.exploration_event_sink = None
            if self.coordinator:
                for task in self.tasks():
                    resource_id = "exploration:" + task.task_id
                    resource = next(
                        (
                            r
                            for r in self.coordinator.store.resources(self.run_id)
                            if r.resource_id == resource_id
                        ),
                        None,
                    )
                    if resource is not None:
                        result = self.reconcile(resource)
                        self.coordinator.transition_resource(
                            resource_id,
                            result.status,
                            cleanup=result.cleanup,
                        )
