"""Workbench execution coordinator with fresh runtimes and durable file states."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agent_runtime.cancellation import CancellationToken, CancelledError
from agent_runtime.model_turn import FinishKind, ModelTurnResult
from agent_runtime.providers.contracts import ProviderError
from agent_runtime.state_root import project_id, workspace_id
from src.workbench.chat import ChatExecutor
from src.workbench.events import EventJournal
from src.workbench.files import ProjectFiles
from src.workbench.models import ConflictError, Run, Turn
from src.workbench.ownership import ServiceOwnership
from src.workbench.project import ProjectExecutor, ProjectResult
from src.workbench.restore import RestoreCoordinator
from src.workbench.store import WorkbenchStore
from src.workbench.supervisor import ProcessWorkerRunner, WorkerFailedError, reconcile_execution
from src.workbench.workspaces import ExecutionWorkspaces

_TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
_LOG = logging.getLogger(__name__)


class UnsupportedModeError(ValueError):
    """Unsupported mode or operation, rejected before any history mutation."""


@dataclass
class _Execution:
    conversation_id: str
    run_id: str
    token: CancellationToken = field(default_factory=CancellationToken)
    thread: threading.Thread | None = None
    # A stream callback and terminal commit must never cross each other.
    # L2 cancellation callbacks synchronously publish progress through this gate.
    gate: threading.RLock = field(default_factory=threading.RLock)
    accepting: bool = True
    parts: list[str] = field(default_factory=list)
    output_bytes: int = 0


class WorkbenchService:
    """Exclusive owner; HTTP, CLI and tests use the same application operations.

    All branch mutations use the service lock. The S1 store remains independently
    readable but direct writes while this coordinator runs are unsupported.
    """

    def __init__(
        self,
        state_dir: str | Path,
        *,
        executor=None,
        project_executor=None,
        max_active_runs: int = 8,
        max_output_bytes: int = 1_000_000,
        project_lock_dir: str | Path | None = None,
        restore_fault=None,
        _worker_runner=None,
    ):
        if max_active_runs < 1 or max_output_bytes < 1:
            raise ValueError("execution limits must be positive")
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._ownership = ServiceOwnership(self.state_dir / "service.lock")
        self._lock = threading.RLock()
        self._changed = threading.Condition()
        self._closing = False
        self._active: dict[str, _Execution] = {}
        self.executor = executor if executor is not None else ChatExecutor()
        self.project_executor = (
            project_executor if project_executor is not None else ProjectExecutor()
        )
        self.max_active_runs = max_active_runs
        self.max_output_bytes = max_output_bytes
        self.worker_runner = _worker_runner or ProcessWorkerRunner()
        try:
            self.store = WorkbenchStore(self.state_dir / "workbench.sqlite3")
            self.journal = EventJournal(self.store)
            self.restoration = RestoreCoordinator(
                self.store, self.state_dir, lock_dir=project_lock_dir, fault=restore_fault
            )
            self.workspaces = ExecutionWorkspaces(self.state_dir, self.restoration.repository)
            self._recover()
            self.restoration.reconcile_committed()
        except BaseException:
            if hasattr(self, "restoration"):
                self.restoration.close()
            self._ownership.close()
            raise

    def _recover(self):
        # Service ownership is only controller exclusion. Reconcile every worker
        # and sandbox resource before reading an output or admitting new execution.
        for conversation in self.store.list_conversations():
            for run in self.store.get_tree(conversation.id).runs:
                resource = self.store.resource(run.id)
                if resource is not None and resource.get("state") != "stopped":
                    stopped = reconcile_execution(resource)
                    resource = {**resource, "state": "stopped" if stopped else "unknown"}
                    self.store.save_resource(run.id, resource)
                elif resource is None and run.status == "running":
                    resource = {
                        "state": "unknown",
                        "backend": "legacy",
                        "run_id": run.id,
                        "project_path": conversation.project_path,
                    }
                    self.store.save_resource(run.id, resource)
                if run.status not in _TERMINAL:
                    snapshot = None
                    unknown = resource is not None and resource["state"] != "stopped"
                    if (
                        not unknown
                        and resource
                        and resource.get("workspace")
                        and self.workspaces.path(run.id).exists()
                    ):
                        snapshot = self.workspaces.capture(run.id, resource["root_identity"])
                    elif not unknown and conversation.project_path:
                        _, files = self.restoration.project(conversation.id, recovery=True)
                        snapshot = self.restoration.repository.save(files)
                    run = self.store.finish_run(
                        conversation.id,
                        run.id,
                        status="interrupted",
                        answer=self.journal.partial_answer(run.id),
                        error="execution_cleanup_unconfirmed"
                        if unknown
                        else "service restarted; previous execution interrupted",
                        artifacts={
                            "isolated": bool(resource and resource.get("workspace")),
                            "recovery_required": unknown,
                        },
                        output_snapshot=snapshot,
                    )

    def _publish(self, run_id, kind, payload):
        event = self.journal.publish(run_id, kind, payload)
        with self._changed:
            self._changed.notify_all()
        return event

    def _open(self):
        if self._closing:
            raise ConflictError("workbench service is shutting down")

    def _chat_only(self, conversation_id):
        conversation = self.store.get_conversation(conversation_id)
        if conversation.project_path is not None:
            raise UnsupportedModeError(
                "project branch operations require a reviewed file restore preview"
            )
        return conversation

    def _admit(self, conversation_id, mode="chat"):
        self._open()
        conversation = self.store.get_conversation(conversation_id)
        if mode not in {"chat", "project_chat", "repair"}:
            raise UnsupportedModeError("unknown execution mode")
        if mode != "chat" and conversation.project_path is None:
            raise UnsupportedModeError("project modes require a project conversation")
        if any(e.conversation_id == conversation_id for e in self._active.values()):
            raise ConflictError("conversation has an active worker")
        if any(r.status not in _TERMINAL for r in self.store.get_tree(conversation_id).runs):
            raise ConflictError("conversation has an unfinished run; restart to reconcile")
        if self.store.resource_blockers(conversation_id):
            raise ConflictError(
                "execution cleanup is unconfirmed; reconcile resources before continuing"
            )
        try:
            self.worker_runner.validate(
                self.project_executor if mode != "chat" else self.executor, mode
            )
        except ValueError as exc:
            raise UnsupportedModeError(str(exc)) from exc
        if conversation.project_path:
            self.restoration.project(conversation_id)
        if len(self._active) >= self.max_active_runs:
            raise ConflictError("workbench execution capacity reached")

    def create_conversation(self, title="新对话", *, project_path=None):
        with self._lock:
            self._open()
            if project_path is not None:
                return self.restoration.create(title, project_path)
            return self.store.create_conversation(title)

    def rename_conversation(self, conversation_id, title, *, expected_revision=None):
        with self._lock:
            self._open()
            self._project_idle(conversation_id)
            return self.store.rename_conversation(
                conversation_id, title, expected_revision=expected_revision
            )

    def send(self, conversation_id, text, *, mode="chat", expected_revision=None) -> Turn:
        with self._lock:
            self._admit(conversation_id, mode)
            return self._schedule(self._append(conversation_id, text, mode, expected_revision))

    def _append(self, cid, text, mode, revision):
        conversation = self.store.get_conversation(cid)
        if conversation.project_path is None:
            return self.store.append_turn(cid, text, mode=mode, expected_revision=revision)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must not be empty")
        if revision is not None and conversation.revision != revision:
            raise ConflictError("conversation revision is stale")
        tree = self.store.get_tree(cid)
        head = next(b.head_node_id for b in tree.branches if b.id == conversation.active_branch_id)
        sid = self.store.get_effective_snapshot(cid, head)
        previous = next(s.manifest for s in tree.snapshots if s.id == sid)
        _, files = self.restoration.project(cid)
        key, manifest = self.restoration.repository.save(files)
        # Restore receipts are history metadata, not a file-state difference.
        manual = manifest["entries"] != previous["entries"]
        return self.store.append_project_turn(
            cid,
            text,
            mode=mode,
            key=key,
            manifest=manifest,
            manual=manual,
            expected_revision=revision,
        )

    def edit(self, conversation_id, node_id, text, *, expected_revision=None, plan_id=None) -> Turn:
        with self._lock:
            if self.store.get_conversation(conversation_id).project_path:
                return self._repeat_project(
                    conversation_id, node_id, text, expected_revision, plan_id
                )
            self._admit(conversation_id)
            self._ordinary_node(conversation_id, node_id)
            return self._schedule(
                self.store.edit_turn(
                    conversation_id, node_id, text, expected_revision=expected_revision
                )
            )

    def regenerate(self, conversation_id, node_id, *, expected_revision=None, plan_id=None) -> Turn:
        with self._lock:
            if self.store.get_conversation(conversation_id).project_path:
                return self._repeat_project(
                    conversation_id, node_id, None, expected_revision, plan_id
                )
            self._admit(conversation_id)
            self._ordinary_node(conversation_id, node_id)
            return self._schedule(
                self.store.regenerate_turn(
                    conversation_id, node_id, expected_revision=expected_revision
                )
            )

    def _repeat_project(self, cid, node_id, text, revision, plan_id):
        path = self.store.get_path(cid, node_id)
        if not path or path[-1].kind != "turn":
            raise ValueError("only turn nodes can be reexecuted")
        original = path[-1]
        self._admit(cid, original.mode)
        if text is None:
            text = original.user_text
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must not be empty")
        conversation = self.store.get_conversation(cid)
        if revision is not None and revision != conversation.revision:
            raise ConflictError("conversation revision is stale")
        plan = self.restoration.plans.get(plan_id)
        if (
            plan is None
            or plan["conversation_id"] != cid
            or plan["action"] != "input"
            or plan.get("input_node") != node_id
        ):
            raise ConflictError(
                "project reexecution requires this node's reviewed input restore plan"
            )
        self.restoration.apply(cid, plan_id)
        # Apply records a fork/checkpoint durably first. A dispatch failure can
        # be retried by sending on that branch without replaying old future.
        return self._schedule(self._append(cid, text, original.mode, None))

    def _ordinary_node(self, conversation_id, node_id):
        path = self.store.get_path(conversation_id, node_id)
        if not path or path[-1].kind != "turn" or path[-1].mode != "chat":
            raise UnsupportedModeError("only ordinary chat turns can be reexecuted here")

    def view(self, conversation_id, node_id, *, expected_revision=None):
        with self._lock:
            self._open()
            self._project_idle(conversation_id)
            return self.store.view_node(
                conversation_id, node_id, expected_revision=expected_revision
            )

    def switch_branch(self, conversation_id, branch_id, *, expected_revision=None):
        with self._lock:
            self._open()
            self._chat_only(conversation_id)
            return self.store.switch_branch(
                conversation_id, branch_id, expected_revision=expected_revision
            )

    def fork(self, conversation_id, node_id, *, name="新分支", expected_revision=None):
        with self._lock:
            self._open()
            self._chat_only(conversation_id)
            return self.store.fork_at(
                conversation_id, node_id, name=name, expected_revision=expected_revision
            )

    def _project_idle(self, conversation_id):
        if self.store.resource_blockers(conversation_id):
            raise ConflictError("execution cleanup is unconfirmed")
        if self.store.get_conversation(conversation_id).project_path is not None:
            if any(e.conversation_id == conversation_id for e in self._active.values()):
                raise ConflictError("conversation has an active worker")
            self.restoration.project(conversation_id)

    def project_status(self, conversation_id):
        with self._lock:
            self._open()
            if self.store.get_conversation(conversation_id).project_path is None:
                return {
                    "manual_changes": False,
                    "pending": [],
                    "candidates": [],
                    "project": None,
                    "execution_blockers": self.store.resource_blockers(conversation_id),
                }
            return self.restoration.status(conversation_id)

    def save_checkpoint(self, conversation_id, **kwargs):
        with self._lock:
            self._open()
            self._project_idle(conversation_id)
            return self.restoration.checkpoint(conversation_id, **kwargs)

    def preview_restore(self, conversation_id, **kwargs):
        with self._lock:
            self._open()
            self._project_idle(conversation_id)
            return self.restoration.preview(conversation_id, **kwargs)

    def apply_restore(self, conversation_id, plan_id):
        with self._lock:
            self._open()
            self._project_idle(conversation_id)
            return self.restoration.apply(conversation_id, plan_id)

    def recover_restore(self, conversation_id, operation_id, *, decision):
        with self._lock:
            self._open()
            if self.store.resource_blockers(conversation_id):
                raise ConflictError("execution cleanup is unconfirmed")
            if any(e.conversation_id == conversation_id for e in self._active.values()):
                raise ConflictError("conversation has an active worker")
            return self.restoration.recover(conversation_id, operation_id, decision=decision)

    def reconcile_resources(self, conversation_id, run_id):
        with self._lock:
            self._open()
            self.store.get_run(conversation_id, run_id)
            if run_id in self._active:
                raise ConflictError("stop the active worker first")
            resource = self.store.resource(run_id)
            if resource is None or not reconcile_execution(resource):
                raise ConflictError("execution cleanup remains unconfirmed; no files changed")
            self.store.save_resource(run_id, {**resource, "state": "stopped"})
            self._publish(run_id, "execution.reconciled", {"run_id": run_id, "state": "stopped"})
            self.restoration.reconcile_committed()
            return {"run_id": run_id, "state": "stopped"}

    def preview_candidate(self, conversation_id, run_id):
        with self._lock:
            self._open()
            self._project_idle(conversation_id)
            return self.restoration.preview_candidate(conversation_id, run_id)

    def discard_candidate(self, conversation_id, run_id):
        with self._lock:
            self._open()
            self._project_idle(conversation_id)
            self.store.discard_candidate(conversation_id, run_id)

    def _schedule(self, turn: Turn) -> Turn:
        execution = _Execution(turn.run.conversation_id, turn.run.id)
        self._active[turn.run.id] = execution
        try:
            conversation = self.store.get_conversation(turn.run.conversation_id)
            resource = {
                "state": "allocated",
                "run_id": turn.run.id,
                "conversation_id": turn.run.conversation_id,
                "node_id": turn.node.id,
                "state_root": str(self.state_dir / "runtime" / turn.run.id),
                "project_path": conversation.project_path,
            }
            if conversation.project_path and turn.node.mode in {"project_chat", "repair"}:
                snapshot = next(
                    s
                    for s in self.store.get_tree(conversation.id).snapshots
                    if s.id == turn.node.input_snapshot_id
                )
                resource.update(
                    project_id=project_id(conversation.project_path),
                    workspace_id=workspace_id(self.workspaces.path(turn.run.id)),
                    input_key=snapshot.storage_key,
                    root_identity=snapshot.manifest["root_identity"],
                    project_revision=self.store.project_record(conversation.id)["revision"],
                    workspace=str(self.workspaces.path(turn.run.id)),
                )
            self.store.save_resource(turn.run.id, resource)
            execution.thread = threading.Thread(
                target=self._execute,
                args=(turn, execution),
                name="workbench-" + turn.run.id,
                daemon=True,
            )
            execution.thread.start()
        except BaseException:
            self._active.pop(turn.run.id, None)
            snapshot = None
            if self.store.get_conversation(turn.run.conversation_id).project_path:
                _, files = self.restoration.project(turn.run.conversation_id)
                snapshot = self.restoration.repository.save(files)
            self.store.finish_run(
                turn.run.conversation_id,
                turn.run.id,
                status="interrupted",
                error="execution could not be dispatched",
                output_snapshot=snapshot,
            )
            raise
        return turn

    def _execute(self, turn, execution):
        status, answer, error, artifacts = "failed", "", None, {}
        input_snapshot, resource = None, self.store.resource(execution.run_id)
        try:
            execution.token.check()
            self.store.start_run(execution.conversation_id, execution.run_id)
            messages = self.store.get_messages(execution.conversation_id, turn.node.id)

            def chunk(text):
                if not isinstance(text, str):
                    raise ValueError("chat delta must be text")
                if not text:
                    return
                with execution.gate:
                    execution.token.check()
                    if not execution.accepting:
                        raise CancelledError("execution already finished")
                    size = len(text.encode("utf-8"))
                    if execution.output_bytes + size > self.max_output_bytes:
                        raise ValueError("chat output limit exceeded")
                    self._publish(execution.run_id, "answer.delta", {"text": text})
                    execution.parts.append(text)
                    execution.output_bytes += size

            if turn.node.mode in {"project_chat", "repair"}:
                tree = self.store.get_tree(execution.conversation_id)
                input_snapshot = next(
                    s for s in tree.snapshots if s.id == turn.node.input_snapshot_id
                )
                manifest = self.restoration.repository.load(
                    input_snapshot.storage_key, input_snapshot.manifest
                )
                root = self.workspaces.create(execution.run_id, manifest, execution.token)
                with closing(ProjectFiles(root)) as files:
                    result = self.worker_runner.run(
                        self.project_executor,
                        mode=turn.node.mode,
                        files=files,
                        messages=messages,
                        token=execution.token,
                        on_chunk=chunk,
                        publish=lambda kind, payload: self._progress(execution, kind, payload),
                        run_id=execution.run_id,
                        state_root=self.state_dir / "runtime" / execution.run_id,
                        store=self.store,
                        resource=resource,
                    )
            else:
                result = self.worker_runner.run(
                    self.executor,
                    mode=turn.node.mode,
                    files=None,
                    messages=messages,
                    token=execution.token,
                    on_chunk=chunk,
                    publish=lambda kind, payload: self._progress(execution, kind, payload),
                    run_id=execution.run_id,
                    state_root=self.state_dir / "runtime" / execution.run_id,
                    store=self.store,
                    resource=resource,
                )
            execution.token.check()
            if isinstance(result, ProjectResult):
                if result.status not in {"succeeded", "failed"} or not result.answer.strip():
                    raise ValueError("invalid project result")
                if not execution.parts:
                    chunk(result.answer)
                if "".join(execution.parts) != result.answer:
                    raise ValueError("project stream does not match final answer")
                status, answer, error, artifacts = (
                    result.status,
                    result.answer,
                    result.error,
                    result.artifacts,
                )
                return
            if (
                not isinstance(result, ModelTurnResult)
                or result.finish.kind != FinishKind.TEXT_COMPLETE
            ):
                raise ValueError("chat executor must return a completed model turn")
            if not result.text.strip():
                raise ValueError("chat returned an empty answer")
            partial = "".join(execution.parts)
            if not partial:
                chunk(result.text)
            elif partial != result.text:
                raise ValueError("streamed text does not match the final answer")
            answer = result.text
            artifacts = {"usage": result.usage, "finish": asdict(result.finish)}
            status = "succeeded"
        except CancelledError:
            status = "interrupted" if execution.token.reason == "shutdown" else "cancelled"
            error = execution.token.reason or "cancelled"
        except Exception as exc:
            # Never store raw exception strings: transports may include credentials.
            error = (
                exc.code.value
                if isinstance(exc, ProviderError)
                else exc.error_code
                if isinstance(exc, WorkerFailedError)
                else type(exc).__name__
            )
            _LOG.warning("workbench run %s failed (%s)", execution.run_id, error)
        finally:
            with execution.gate:
                execution.accepting = False
                # Serialize the last cancellation check with the terminal commit.
                if execution.token.is_cancelled and status == "succeeded":
                    status = "interrupted" if execution.token.reason == "shutdown" else "cancelled"
                    error = execution.token.reason
                if status != "succeeded":
                    answer = "".join(execution.parts)
                try:
                    snapshot = None
                    candidate = None
                    latest_resource = self.store.resource(execution.run_id)
                    unknown = latest_resource and latest_resource["state"] == "unknown"
                    if unknown:
                        status, error = "interrupted", "execution_cleanup_unconfirmed"
                        artifacts.update(
                            recovery_required=True, execution_state="unknown", isolated=True
                        )
                    elif (
                        input_snapshot is not None
                        and self.workspaces.path(execution.run_id).exists()
                    ):
                        snapshot = self.workspaces.capture(
                            execution.run_id, resource["root_identity"]
                        )
                        before, after = input_snapshot.manifest, snapshot[1]
                        artifacts["isolated"] = True
                        artifacts["changed_files"] = [
                            p
                            for p in sorted(set(before["entries"]) | set(after["entries"]))
                            if before["entries"].get(p) != after["entries"].get(p)
                        ]
                        if (
                            status == "succeeded"
                            and turn.node.mode == "repair"
                            and artifacts["changed_files"]
                        ):
                            candidate = resource
                    elif self.store.get_conversation(execution.conversation_id).project_path:
                        _, files = self.restoration.project(execution.conversation_id)
                        snapshot = self.restoration.repository.save(files)
                        tree = self.store.get_tree(execution.conversation_id)
                        before = next(
                            s.manifest
                            for s in tree.snapshots
                            if s.id == turn.node.input_snapshot_id
                        )
                        after = snapshot[1]
                        artifacts["changed_files"] = [
                            p
                            for p in sorted(set(before["entries"]) | set(after["entries"]))
                            if before["entries"].get(p) != after["entries"].get(p)
                        ]
                    self.store.finish_run(
                        execution.conversation_id,
                        execution.run_id,
                        status=status,
                        answer=answer,
                        error=error,
                        artifacts=artifacts,
                        output_snapshot=snapshot,
                        candidate=candidate,
                    )
                except Exception:
                    _LOG.exception("could not persist workbench terminal state")
            with self._lock:
                self._active.pop(execution.run_id, None)
            with self._changed:
                self._changed.notify_all()

    def _progress(self, execution, kind, payload):
        with execution.gate:
            if execution.accepting and (
                not execution.token.is_cancelled or kind == "run.cancelling"
            ):
                self._publish(execution.run_id, kind, payload)

    def stop(self, conversation_id, run_id) -> Run:
        with self._lock:
            self._open()
            run = self.store.get_run(conversation_id, run_id)
            execution = self._active.get(run_id)
            if run.status in _TERMINAL:
                return run
            if execution is None:
                raise ConflictError("run is not owned by this service")
            with execution.gate:
                if execution.accepting and not execution.token.is_cancelled:
                    self._publish(run_id, "run.stop_requested", {"reason": "user"})
                    execution.token.cancel("user")
            return self.store.get_run(conversation_id, run_id)

    def events(self, conversation_id, run_id, *, after=0):
        self.store.get_run(conversation_id, run_id)
        if not isinstance(after, int) or after < 0 or after > self.journal.latest(run_id):
            raise ValueError("event cursor is outside this run")
        return self.journal.read(run_id, after)

    def wait(self, conversation_id, run_id, *, timeout=5) -> Run:
        deadline = time.monotonic() + timeout
        while True:
            run = self.store.get_run(conversation_id, run_id)
            with self._lock:
                if run.status in _TERMINAL and run_id not in self._active:
                    return run
            if time.monotonic() >= deadline:
                raise TimeoutError("run has not finished")
            self.wait_for_change(min(0.05, max(0, deadline - time.monotonic())))

    def wait_for_change(self, timeout=0.25):
        with self._changed:
            self._changed.wait(timeout)

    def close(self, *, timeout=5):
        with self._lock:
            self._closing = True
            executions = list(self._active.values())
            for execution in executions:
                with execution.gate:
                    execution.token.cancel("shutdown")
        deadline = time.monotonic() + timeout
        for execution in executions:
            if execution.thread is not None:
                execution.thread.join(max(0, deadline - time.monotonic()))
        with self._lock:
            if self._active:
                raise TimeoutError("workers have not exited; service ownership retained")
            self.restoration.close()
            self._ownership.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
