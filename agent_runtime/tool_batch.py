"""Native independent read calls: bounded dispatch, identity and cancellation."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import copy, deepcopy
from dataclasses import dataclass, field

from agent_runtime.cancellation import CancellationToken
from agent_runtime.tool_context import ToolContext
from agent_runtime.tool_result import ToolResult, attach_tool_receipt, require_tool_result
from agent_runtime.tool_schema import validate_tool_arguments

PARALLEL_READ_TOOLS = frozenset({"read_file", "list_files"})


class ToolBatchProtocolError(ValueError):
    code = "tool_batch_protocol_error"


def validate_native_content(calls, content):
    """Check raw/normalized pairing without repairing or authorizing calls."""
    if content is None or content == []:
        return
    if not isinstance(content, list) or any(not isinstance(block, dict) for block in content):
        raise ToolBatchProtocolError("invalid native content structure")
    blocks = [block for block in content if block.get("type") == "tool_use"]
    if len(blocks) != len(calls):
        raise ToolBatchProtocolError("native call/content count mismatch")
    for call, block in zip(calls, blocks, strict=True):
        if (
            block.get("id") != call.call_id
            or block.get("name") != call.name
            or not isinstance(block.get("input"), dict)
        ):
            raise ToolBatchProtocolError("native call/content identity mismatch")
        try:
            raw = json.dumps(block["input"], allow_nan=False, sort_keys=True)
            arguments = json.dumps(call.arguments, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ToolBatchProtocolError("invalid native content arguments") from exc
        if raw != arguments:
            raise ToolBatchProtocolError("native call/content arguments mismatch")


@dataclass
class ToolCallContext:
    run_id: str
    turn_id: str
    batch_id: str
    call_id: str
    ordinal: int
    arguments_hash: str
    idempotency_key: str
    tool_context: ToolContext
    cancel_token: CancellationToken = field(default_factory=CancellationToken)
    isolated: bool = False
    budget_reserved: bool = False
    timeout_s: float = 0.0
    registry: dict = field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()


@dataclass
class BatchCall:
    call_id: str
    ordinal: int
    tool_name: str
    arguments: dict
    context: ToolCallContext
    status: str = "queued"
    result: ToolResult | None = None
    result_ref: str = ""
    argument_errors: list[dict] = field(default_factory=list)

    def argument_rejection(self):
        if not self.argument_errors:
            return None
        return ToolResult(
            content=f"Error: 参数预检失败: {self.argument_errors}",
            status="rejected",
            error_code="invalid_arguments",
            retryable=True,
            metadata={
                "preflight": True,
                "rejection_reason": "invalid_args",
                "structured_facts": [
                    {
                        "kind": "argument_preflight",
                        "status": "rejected",
                        "errors": deepcopy(self.argument_errors),
                    }
                ],
            },
        )

    def checkpoint(self):
        return {
            "call_id": self.call_id,
            "ordinal": self.ordinal,
            "tool_name": self.tool_name,
            "status": self.status,
            "result_ref": self.result_ref,
            "receipt": dict(self.result.receipt) if self.result else {},
        }


@dataclass
class ToolCallBatch:
    batch_id: str
    run_id: str
    turn_id: str
    calls: list[BatchCall]
    parallel: bool
    downgrade_reason: str = ""
    status: str = "pending"

    @classmethod
    def create(
        cls, calls, *, run_id, turn_id, context, registry, allowed_tools=None, native_content=None
    ):
        registry = {name: dict(spec) for name, spec in registry.items()}
        for spec in registry.values():
            for key in ("schema",):
                if key in spec:
                    spec[key] = deepcopy(spec[key])
        if not isinstance(calls, list) or not calls:
            raise ToolBatchProtocolError("invalid batch structure")
        ids = [getattr(call, "call_id", None) for call in calls]
        if any(not isinstance(cid, str) or not cid.strip() for cid in ids):
            raise ToolBatchProtocolError("empty call ID")
        if len(set(ids)) != len(ids):
            raise ToolBatchProtocolError("duplicate call ID")
        for call in calls:
            if (
                not isinstance(getattr(call, "arguments", None), dict)
                or not isinstance(getattr(call, "name", None), str)
                or call.name not in registry
            ):
                raise ToolBatchProtocolError("unknown tool or invalid arguments structure")
            try:
                json.dumps(call.arguments, allow_nan=False, sort_keys=True)
            except (ValueError, TypeError) as exc:
                raise ToolBatchProtocolError("invalid arguments structure") from exc
        validate_native_content(calls, native_content)
        parallel = len(calls) <= 4 and all(
            call.name in PARALLEL_READ_TOOLS
            and registry[call.name].get("side_effect") == "read"
            and not registry[call.name].get("risky")
            and not registry[call.name].get("terminal")
            and callable(registry[call.name].get("run_with_context"))
            # Replaced execution handlers require a fresh explicit audit.
            and registry[call.name].get("parallel_run") is registry[call.name].get("run")
            for call in calls
        )
        batch_id = "batch-" + uuid.uuid4().hex
        items = []
        for ordinal, call in enumerate(calls):
            arguments = json.loads(json.dumps(call.arguments))
            spec = registry[call.name]
            # Preserve raw arguments and identities; Executor owns normalization and gates.
            _, argument_errors = validate_tool_arguments(spec["schema"], deepcopy(arguments))
            args_hash = hashlib.sha256(
                json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            isolated_context = copy(context) if parallel else context
            if parallel:
                isolated_context.observation_state = None
                isolated_context.exploration_service = None
                isolated_context.edit_lock = None
                isolated_context.grounding_sink = None
                isolated_context.sandbox_identity = dict(context.sandbox_identity)
                if getattr(context.path_resolver, "__self__", None) is context:
                    isolated_context.path_resolver = isolated_context._default_resolve
            call_context = ToolCallContext(
                run_id,
                turn_id,
                batch_id,
                call.call_id,
                ordinal,
                args_hash,
                f"{run_id}:{turn_id}:{batch_id}:{call.call_id}:{args_hash}",
                isolated_context,
                isolated=parallel,
                registry=registry,
                allowed_tools=tuple(registry) if allowed_tools is None else tuple(allowed_tools),
            )
            items.append(
                BatchCall(
                    call.call_id,
                    ordinal,
                    call.name,
                    arguments,
                    call_context,
                    argument_errors=argument_errors,
                )
            )
        return cls(
            batch_id,
            run_id,
            turn_id,
            items,
            parallel,
            "capacity" if len(calls) > 4 else "" if parallel else "serial_policy",
        )


def call_result(call: BatchCall, result) -> ToolResult:
    result = require_tool_result(result, tool_name=call.tool_name)
    result.metadata.update(
        {
            "turn_id": call.context.turn_id,
            "batch_id": call.context.batch_id,
            "call_id": call.call_id,
            "ordinal": call.ordinal,
            "idempotency_key": call.context.idempotency_key,
        }
    )
    from agent_runtime.tool_executor import _canonical_args_hash

    return attach_tool_receipt(
        result,
        call.tool_name,
        args_hash=_canonical_args_hash(call.tool_name, call.arguments),
        run_id=call.context.run_id,
        call_id=call.call_id,
    )


def cancellation_result(*, uncertain=False):
    return ToolResult(
        content="Error: tool batch cancelled",
        status="uncertain" if uncertain else "cancelled",
        error_code="cleanup_unverified" if uncertain else "tool_cancelled",
        metadata={"termination_guaranteed": not uncertain},
    )


class ToolBatchScheduler:
    """Prepare and settle on the owner thread; workers only execute calls.

    ``prepare`` returns a callable, or a preflight rejection ToolResult.
    ``settle`` records observations in model order, including cancellation.
    Abandoned threads retain their permit until they actually exit.
    """

    def __init__(self, permits, emitter, *, cleanup_s=0.25, on_result=None):
        self.permits = permits
        self.emitter = emitter
        self.cleanup_s = cleanup_s
        self.on_result = on_result

    def event(self, kind, call, **fields):
        self.emitter.emit(
            kind,
            batch_id=call.context.batch_id,
            call_id=call.call_id,
            ordinal=call.ordinal,
            tool_name=call.tool_name,
            status=call.status,
            **fields,
        )

    def finish(self, call, result):
        if self.on_result is not None:
            result = self.on_result(call, require_tool_result(result))
        call.result = call_result(call, result)
        status = call.result.status
        call.status = (
            "succeeded"
            if status == "success"
            else (status if status in {"rejected", "cancelled", "uncertain"} else "failed")
        )
        kind = {"cancelled": "tool_call_cancelled", "uncertain": "tool_call_uncertain"}.get(
            call.status, "tool_call_completed"
        )
        self.event(
            kind, call, error_code=call.result.error_code, duration_ms=call.result.duration_ms
        )

    def run(self, batch, prepare, settle, *, cancel_token=None, expired=None):
        def cancel_calls():
            for call in batch.calls:
                call.context.cancel_token.cancel("batch_cancelled")

        unsubscribe = cancel_token.add_callback(cancel_calls) if cancel_token is not None else None
        try:
            return self._run(batch, prepare, settle, cancel_token=cancel_token, expired=expired)
        finally:
            if unsubscribe is not None:
                unsubscribe()

    def _run(self, batch, prepare, settle, *, cancel_token=None, expired=None):
        self.emitter.emit(
            "tool_batch_created",
            batch_id=batch.batch_id,
            status="pending",
            parallel=batch.parallel,
            downgrade_reason=batch.downgrade_reason,
            call_count=len(batch.calls),
        )
        batch.status = "running"
        for call in batch.calls:
            self.event("tool_call_queued", call, reason="waiting_capacity")
        if not batch.parallel:
            outputs = []
            for call in batch.calls:
                if cancel_token is not None and cancel_token.is_cancelled:
                    self.finish(call, cancellation_result())
                else:
                    call.status = "running"
                    self.event("tool_call_started", call)
                    work = prepare(call)
                    self.finish(call, work() if callable(work) else work)
                outputs.append(settle(call, call.result))
            return outputs

        inherited = self.permits.inherited
        width = 1 if inherited else 2
        pool = ThreadPoolExecutor(max_workers=width, thread_name_prefix="tool-batch")
        active = {}
        started = {}
        timeouts = {}
        pending = list(batch.calls)
        cancelling_at = None
        abandoned = 0

        def execute(call, work, release):
            try:
                if call.context.cancel_token.is_cancelled:
                    return cancellation_result()
                started = time.monotonic()
                result = require_tool_result(work(), tool_name=call.tool_name)
                result.duration_ms = int((time.monotonic() - started) * 1000)
                return result
            except Exception:
                return ToolResult(
                    content="Error: tool execution failed",
                    status="error",
                    error_code="tool_execution_failed",
                )
            finally:
                release()

        try:
            while pending or active:
                cancelled = (cancel_token is not None and cancel_token.is_cancelled) or (
                    expired is not None and expired()
                )
                if cancelled and cancelling_at is None:
                    cancelling_at = time.monotonic()
                    for call in pending:
                        self.finish(call, cancellation_result())
                    pending.clear()
                    for call in active.values():
                        call.context.cancel_token.cancel("batch_cancelled")
                        self.event("tool_call_cancel_requested", call)
                while pending and cancelling_at is None and len(active) < width:
                    if not inherited and not self.permits.acquire():
                        break
                    call = pending.pop(0)
                    try:
                        work = prepare(call)
                    except BaseException:
                        if not inherited:
                            self.permits.release()
                        raise
                    if not callable(work):
                        if not inherited:
                            self.permits.release()
                        self.finish(call, work)
                        continue
                    call.status = "running"
                    self.event("tool_call_started", call)
                    release = self.permits.borrow() if inherited else self.permits.release
                    future = pool.submit(execute, call, work, release)
                    active[future] = call
                    started[future] = time.monotonic()
                if active:
                    done, _ = wait(active, timeout=0.01, return_when=FIRST_COMPLETED)
                    for future in done:
                        call = active.pop(future)
                        result = future.result()
                        if future in timeouts:
                            result = ToolResult(
                                content="Error: tool deadline exceeded",
                                status="error",
                                error_code="tool_timeout",
                                metadata={"termination_guaranteed": True},
                            )
                        elif (
                            cancelling_at is not None
                            or call.context.cancel_token.is_cancelled
                            or (cancel_token is not None and cancel_token.is_cancelled)
                        ):
                            # The worker actually returned. Its tool backend
                            # may still report unconfirmed nested cleanup.
                            result = cancellation_result(
                                uncertain=(result.metadata.get("termination_guaranteed") is False)
                            )
                        self.finish(call, result)
                    for future, call in list(active.items()):
                        if (
                            call.context.timeout_s > 0
                            and future not in timeouts
                            and time.monotonic() - started[future] >= call.context.timeout_s
                        ):
                            timeouts[future] = time.monotonic()
                            call.context.cancel_token.cancel("tool_timeout")
                            self.event(
                                "tool_call_cancel_requested", call, error_code="tool_timeout"
                            )
                        if (
                            future in timeouts
                            and time.monotonic() - timeouts[future] >= self.cleanup_s
                        ):
                            self.finish(
                                call,
                                ToolResult(
                                    content="Error: tool timeout cleanup unverified",
                                    status="uncertain",
                                    error_code="tool_timeout",
                                    metadata={"termination_guaranteed": False},
                                ),
                            )
                            active.pop(future)
                            abandoned += 1
                            if abandoned >= width:
                                for queued in pending:
                                    self.finish(queued, cancellation_result())
                                pending.clear()
                else:
                    time.sleep(0.01)
                if cancelling_at is not None and time.monotonic() - cancelling_at >= self.cleanup_s:
                    for call in active.values():
                        self.finish(call, cancellation_result(uncertain=True))
                    active.clear()
            return [settle(call, call.result) for call in batch.calls]
        finally:
            for call in active.values():
                call.context.cancel_token.cancel("batch_closed")
            pool.shutdown(wait=False)
