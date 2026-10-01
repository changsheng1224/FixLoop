"""Atomic exploration batches and generation-fenced durable worker receipts."""

from __future__ import annotations

import json
import time
from contextlib import closing

from agent_runtime.plan_runtime.models import new_id
from src.collaboration.contracts import AgentTask, TaskStatus
from src.collaboration.exploration_contracts import ACTIVE, KINDS
from src.collaboration.store import CollaborationStore, LeaseConflictError


class ExplorationStore(CollaborationStore):
    def _event(self, conn, run_id, object_id, event_type, payload):
        super()._event(conn, run_id, object_id, event_type, payload)
        sink = getattr(self, "exploration_event_sink", None)
        if sink and event_type.startswith(("subagent_", "exploration_batch_")):
            try:
                sink(event_type, payload)
            except Exception:
                pass  # Progress delivery is not the durable dispatch boundary.

    @staticmethod
    def _progress(task, event):
        data = task.payload["exploration"]
        return {
            "event": event,
            "run_id": task.run_id,
            "parent_task_id": task.parent_task_id,
            "turn_id": data["turn_id"],
            "call_id": task.task_id,
            "task_id": task.task_id,
            "attempt_id": data.get("attempt_id", ""),
            "lease_generation": data["lease_generation"],
            "workspace_id": data["workspace_id"],
            "plan_id": data["plan_id"],
            "plan_version": data["plan_version"],
            "node_id": data["node_id"],
            "status": data["status"],
            "tool_name": task.kind,
            "budget": task.budget,
            "duration_ms": int(
                1000
                * max(0, data.get("ended_at", time.time()) - data.get("started_at", time.time()))
            ),
        }

    def _save(self, conn, task, event):
        task.version += 1
        task.updated_at = time.time()
        conn.execute(
            "UPDATE task_records SET payload=?, version=?, status=?, lease_owner=?, "
            "lease_expires_at=?, updated_at=? WHERE task_id=?",
            (
                self._json(task.to_dict()),
                task.version,
                str(task.status),
                task.lease_owner,
                task.lease_expires_at,
                task.updated_at,
                task.task_id,
            ),
        )
        self._event(conn, task.run_id, task.task_id, event, self._progress(task, event))

    def submit_batch(self, tasks: list[AgentTask], *, run_token_limit: int):
        if not tasks or len(tasks) > 2 or len({t.task_id for t in tasks}) != len(tasks):
            raise ValueError("invalid_exploration_batch")
        if len({t.kind for t in tasks}) != len(tasks) or len({t.run_id for t in tasks}) != 1:
            raise ValueError("invalid_exploration_batch_identity")
        for task in tasks:
            if (
                task.validate()
                or task.role != "explorer"
                or task.depends_on
                or task.kind not in KINDS
            ):
                raise ValueError("invalid_exploration_task")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT payload FROM task_records WHERE run_id=?", (tasks[0].run_id,)
            )
            existing = [AgentTask.from_dict(json.loads(row["payload"])) for row in rows]
            explorations = [t for t in existing if "exploration" in t.payload]
            active = [
                t
                for t in explorations
                if t.payload["exploration"]["status"] in ACTIVE
                and not t.payload["exploration"].get("cleanup_confirmed")
            ]
            if len(active) + len(tasks) > 2:
                raise ValueError("exploration_concurrency_limit")
            used = sum(t.payload["exploration"]["charged_tokens"] for t in explorations)
            if used + sum(t.budget["tokens"] for t in tasks) > run_token_limit:
                raise ValueError("exploration_run_budget_exhausted")
            for task in tasks:
                task.version = 1
                conn.execute(
                    "INSERT INTO task_records(task_id,run_id,payload,version,status,priority,"
                    "lease_owner,lease_expires_at,updated_at) VALUES (?,?,?,?,?,0,'',0,?)",
                    (
                        task.task_id,
                        task.run_id,
                        self._json(task.to_dict()),
                        1,
                        str(task.status),
                        task.updated_at,
                    ),
                )
                self._event(
                    conn,
                    task.run_id,
                    task.task_id,
                    "subagent_queued",
                    self._progress(task, "subagent_queued"),
                )
        return tasks

    def mutate(self, task_id, callback, event):
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM task_records WHERE task_id=?", (task_id,)
            ).fetchone()
            if not row:
                raise KeyError(task_id)
            task = AgentTask.from_dict(json.loads(row["payload"]))
            if callback(task, conn) is not False:
                self._save(conn, task, event)
            return task

    def claim(self, task_id, owner):
        def claim(task, conn):
            data = task.payload["exploration"]
            if data["status"] != "queued" or data.get("cancel_requested"):
                raise LeaseConflictError("exploration_not_claimable")
            if time.time() >= task.deadline_at:
                raise LeaseConflictError("exploration_deadline_expired")
            task.status = TaskStatus.RUNNING
            task.attempt += 1
            task.max_attempts = max(task.max_attempts, task.attempt)
            data.update(
                status="running",
                attempt_id=new_id("explore-attempt"),
                lease_generation=data["lease_generation"] + 1,
                owner=owner,
                started_at=time.time(),
                cleanup_confirmed=False,
            )
            task.lease_owner = data["attempt_id"]
            task.lease_expires_at = task.deadline_at + 5

        return self.mutate(task_id, claim, "subagent_started")

    def finish(self, claimed, result):
        def finish(task, conn):
            data, expected = task.payload["exploration"], claimed.payload["exploration"]
            if (
                data["status"] != "running"
                or data["attempt_id"] != expected["attempt_id"]
                or data["lease_generation"] != expected["lease_generation"]
            ):
                raise LeaseConflictError("exploration_attempt_fenced")
            if data.get("cancel_requested"):
                result["status"] = "cancelled"
            status = result["status"]
            charge = result.get("usage", {}).get("tokens")
            charge = task.budget["tokens"] if charge is None else max(0, charge)
            data["charged_tokens"] += charge - task.budget["tokens"]
            receipt = {
                "attempt_id": data["attempt_id"],
                "lease_generation": data["lease_generation"],
                "reserved_tokens": task.budget["tokens"],
                "charged_tokens": charge,
                "usage_known": result.get("usage", {}).get("tokens") is not None,
                "model_turns": result.get("usage", {}).get("model_turns", 3),
                "tool_calls": result.get("usage", {}).get("tool_calls", 4),
            }
            data["reservation_settled"] = True
            data.setdefault("receipts", []).append(receipt)
            data.update(
                status=status,
                result=result,
                result_ref=new_id("explore-result"),
                cleanup_confirmed=True,
                ended_at=time.time(),
            )
            task.status = (
                TaskStatus.COMPLETED
                if status in {"completed", "partial"}
                else TaskStatus.CANCELLED
                if status == "cancelled"
                else TaskStatus.FAILED
            )
            task.lease_owner, task.lease_expires_at = "", 0

        return self.mutate(claimed.task_id, finish, "subagent_" + result["status"])

    def invalidate_worker(self, task_id, *, reason="cancelled", stopped=False):
        def invalidate(task, conn):
            data = task.payload["exploration"]
            if data["status"] not in ACTIVE:
                return False
            was_queued = data["status"] == "queued"
            data["cancel_requested"] = reason
            data["lost_attempt_id"] = data.get("attempt_id", "")
            data["lease_generation"] += 1
            data["status"] = reason if was_queued or stopped else "worker_lost"
            data["cleanup_confirmed"] = was_queued or stopped
            task.status = TaskStatus.CANCELLED if data["cleanup_confirmed"] else TaskStatus.EXPIRED
            task.lease_owner, task.lease_expires_at = "", 0
            if not data.get("reservation_settled"):
                data["reservation_settled"] = True
                charge = 0 if was_queued else task.budget["tokens"]
                data["charged_tokens"] += charge - task.budget["tokens"]
                data.setdefault("receipts", []).append(
                    {
                        "attempt_id": data.get("attempt_id", ""),
                        "charged_tokens": charge,
                        "reserved_tokens": task.budget["tokens"],
                        "usage_known": was_queued,
                        "lease_generation": data["lease_generation"],
                        "model_turns": 0 if was_queued else 3,
                        "tool_calls": 0 if was_queued else 4,
                    }
                )

        return self.mutate(task_id, invalidate, "subagent_cancel_requested")

    def confirm_cleanup(self, task_id, attempt_id):
        def confirm(task, conn):
            data = task.payload["exploration"]
            if data.get("lost_attempt_id") != attempt_id:
                return False
            data["cleanup_confirmed"] = True
            data["status"] = data.get("cancel_requested", "cancelled")

        return self.mutate(task_id, confirm, "subagent_cleanup_checked")

    def retry(self, task_id, *, versions, deadline_s, run_token_limit, stopped):
        def retry(task, conn):
            data = task.payload["exploration"]
            if data["status"] not in {"running", "worker_lost"}:
                return False
            if not stopped(task):
                raise LeaseConflictError("old_exploration_worker_unconfirmed")
            rows = conn.execute("SELECT payload FROM task_records WHERE run_id=?", (task.run_id,))
            others = [AgentTask.from_dict(json.loads(r["payload"])) for r in rows]
            used = sum(t.payload.get("exploration", {}).get("charged_tokens", 0) for t in others)
            if used + task.budget["tokens"] > run_token_limit:
                raise ValueError("exploration_run_budget_exhausted")
            if not data.get("reservation_settled"):
                data.setdefault("receipts", []).append(
                    {
                        "attempt_id": data["attempt_id"],
                        "charged_tokens": task.budget["tokens"],
                        "reserved_tokens": task.budget["tokens"],
                        "usage_known": False,
                        "lease_generation": data["lease_generation"],
                    }
                )
            data["charged_tokens"] += task.budget["tokens"]
            data.update(
                status="queued",
                lease_generation=data["lease_generation"] + 1,
                workspace_revision=versions,
                reservation_settled=False,
                cleanup_confirmed=False,
                cancel_requested="",
                result=None,
            )
            task.status = TaskStatus.READY
            task.deadline_at = time.time() + deadline_s
            task.lease_owner, task.lease_expires_at = "", 0

        return self.mutate(task_id, retry, "subagent_queued")

    def emit(self, task, event, **fields):
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM task_records WHERE task_id=?", (task.task_id,)
            ).fetchone()
            if row:
                current = AgentTask.from_dict(json.loads(row["payload"]))
                data, expected = current.payload["exploration"], task.payload["exploration"]
                if expected.get("attempt_id") and (
                    data.get("attempt_id") != expected["attempt_id"]
                    or data["lease_generation"] != expected["lease_generation"]
                ):
                    return  # Late worker progress must not revive a fenced attempt.
                task = current
            payload = self._progress(task, event)
            payload.update(
                {
                    k: v
                    for k, v in fields.items()
                    if k in {"model_turn", "tool_call"} and type(v) is int
                }
            )
            self._event(conn, task.run_id, task.task_id, event, payload)

    def progress_events(self, run_id):
        events = []
        sequences = {}
        for record in self.events(run_id=run_id):
            if record["event_type"].startswith(("subagent_", "exploration_batch_")):
                turn = record["payload"]["turn_id"]
                sequences[turn] = sequences.get(turn, 0) + 1
                events.append({**record["payload"], "event_seq": sequences[turn]})
        return events
