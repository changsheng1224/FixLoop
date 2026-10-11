"""S2 execution coordinator: one durable turn per conversation, fresh chat context."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agent_runtime.cancellation import CancellationToken, CancelledError
from agent_runtime.model_turn import FinishKind, ModelTurnResult
from agent_runtime.providers.contracts import ProviderError
from src.workbench.chat import ChatExecutor
from src.workbench.events import EventJournal
from src.workbench.models import ConflictError, Run, Turn
from src.workbench.ownership import ServiceOwnership
from src.workbench.store import WorkbenchStore

_TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
_LOG = logging.getLogger(__name__)


class UnsupportedModeError(ValueError):
    """Project execution and restoration need S3/S4, before any history mutation."""


@dataclass
class _Execution:
    conversation_id: str
    run_id: str
    token: CancellationToken = field(default_factory=CancellationToken)
    thread: threading.Thread | None = None
    # A stream callback and terminal commit must never cross each other.
    gate: threading.Lock = field(default_factory=threading.Lock)
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
        max_active_runs: int = 8,
        max_output_bytes: int = 1_000_000,
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
        self.max_active_runs = max_active_runs
        self.max_output_bytes = max_output_bytes
        try:
            self.store = WorkbenchStore(self.state_dir / "workbench.sqlite3")
            self.journal = EventJournal(self.state_dir / "events.sqlite3")
            self._recover()
        except BaseException:
            self._ownership.close()
            raise

    def _recover(self):
        # Ownership proves no previous service worker can still commit to this DB.
        # Tool-free detached model I/O from a closed service is separately fenced.
        for conversation in self.store.list_conversations():
            for run in self.store.get_tree(conversation.id).runs:
                if run.status not in _TERMINAL:
                    run = self.store.finish_run(
                        conversation.id,
                        run.id,
                        status="interrupted",
                        answer=self.journal.partial_answer(run.id),
                        error="service restarted; previous execution interrupted",
                    )
                events = self.journal.read(run.id, max(0, self.journal.latest(run.id) - 1))
                if not events or events[-1]["kind"] != "run.finished":
                    self._publish(run.id, "run.finished", asdict(run))

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
            raise UnsupportedModeError("project sessions require S3 file recovery and S4 execution")
        return conversation

    def _admit(self, conversation_id, mode="chat"):
        self._open()
        self._chat_only(conversation_id)
        if mode != "chat":
            raise UnsupportedModeError("S2 supports ordinary chat only")
        if len(self._active) >= self.max_active_runs:
            raise ConflictError("workbench execution capacity reached")

    def create_conversation(self, title="新对话", *, project_path=None):
        with self._lock:
            self._open()
            if project_path is not None:
                raise UnsupportedModeError("S2 creates projectless conversations only")
            return self.store.create_conversation(title)

    def rename_conversation(self, conversation_id, title, *, expected_revision=None):
        with self._lock:
            self._open()
            return self.store.rename_conversation(
                conversation_id, title, expected_revision=expected_revision
            )

    def send(self, conversation_id, text, *, mode="chat", expected_revision=None) -> Turn:
        with self._lock:
            self._admit(conversation_id, mode)
            return self._schedule(
                self.store.append_turn(conversation_id, text, expected_revision=expected_revision)
            )

    def edit(self, conversation_id, node_id, text, *, expected_revision=None) -> Turn:
        with self._lock:
            self._admit(conversation_id)
            self._ordinary_node(conversation_id, node_id)
            return self._schedule(
                self.store.edit_turn(
                    conversation_id, node_id, text, expected_revision=expected_revision
                )
            )

    def regenerate(self, conversation_id, node_id, *, expected_revision=None) -> Turn:
        with self._lock:
            self._admit(conversation_id)
            self._ordinary_node(conversation_id, node_id)
            return self._schedule(
                self.store.regenerate_turn(
                    conversation_id, node_id, expected_revision=expected_revision
                )
            )

    def _ordinary_node(self, conversation_id, node_id):
        path = self.store.get_path(conversation_id, node_id)
        if not path or path[-1].kind != "turn" or path[-1].mode != "chat":
            raise UnsupportedModeError("only ordinary chat turns can be reexecuted in S2")

    def view(self, conversation_id, node_id, *, expected_revision=None):
        with self._lock:
            self._open()
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

    def _schedule(self, turn: Turn) -> Turn:
        execution = _Execution(turn.run.conversation_id, turn.run.id)
        self._active[turn.run.id] = execution
        try:
            self._publish(turn.run.id, "run.queued", asdict(turn.run))
            execution.thread = threading.Thread(
                target=self._execute,
                args=(turn, execution),
                name="workbench-" + turn.run.id,
                daemon=True,
            )
            execution.thread.start()
        except BaseException:
            self._active.pop(turn.run.id, None)
            run = self.store.finish_run(
                turn.run.conversation_id,
                turn.run.id,
                status="interrupted",
                error="execution could not be dispatched",
            )
            self._publish(run.id, "run.finished", asdict(run))
            raise
        return turn

    def _execute(self, turn, execution):
        status, answer, error, artifacts = "failed", "", None, {}
        try:
            execution.token.check()
            run = self.store.start_run(execution.conversation_id, execution.run_id)
            self._publish(run.id, "run.started", asdict(run))
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

            result = self.executor(messages, execution.token, chunk)
            execution.token.check()
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
            error = exc.code.value if isinstance(exc, ProviderError) else type(exc).__name__
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
                    run = self.store.finish_run(
                        execution.conversation_id,
                        execution.run_id,
                        status=status,
                        answer=answer,
                        error=error,
                        artifacts=artifacts,
                    )
                    self._publish(run.id, "run.finished", asdict(run))
                except Exception:
                    _LOG.exception("could not persist workbench terminal state")
            with self._lock:
                self._active.pop(execution.run_id, None)
            with self._changed:
                self._changed.notify_all()

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
            self._ownership.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
