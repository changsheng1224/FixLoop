"""Batch lifecycle wiring with explicit owner callbacks, not an AgentLoop dependency."""

import threading
from collections.abc import Callable, Generator
from dataclasses import dataclass

from agent_runtime.batch_reads import recheck_read
from agent_runtime.read_permits import read_permits
from agent_runtime.tool_batch import (
    BatchCall,
    ToolBatchProtocolError,
    ToolBatchScheduler,
    ToolCallBatch,
)
from agent_runtime.tool_result import ToolResult

StepFlow = Generator[ToolResult | None, ToolResult, str]
PlanOperation = Generator[None, ToolResult, ToolResult]


@dataclass(frozen=True)
class SettledToolStep:
    content: str
    observation_ref: str


@dataclass(frozen=True)
class BatchExecutionHooks:
    step_flow: Callable[[BatchCall, ToolResult | None], StepFlow]
    settle_step: Callable[[StepFlow, ToolResult], SettledToolStep]
    execute_serial: Callable[[BatchCall], ToolResult]
    plan_operation: Callable[[BatchCall], PlanOperation] | None
    dispatch: Callable[[str, Callable[[], ToolResult]], ToolResult]
    activate: Callable[[ToolCallBatch], None]
    acting: Callable[[BatchCall], None]
    uncertain: Callable[[], None]


class ToolBatchRunner:
    def __init__(
        self,
        *,
        context,
        registry,
        allowed_tools,
        executor,
        progress,
        session,
        hooks,
        cancel_token,
        expired,
        tool_timeout_s,
        emit,
    ):
        self.context = context
        self.registry = registry
        self.allowed_tools = allowed_tools
        self.executor = executor
        self.progress = progress
        self.session = session
        self.hooks = hooks
        self.cancel_token = cancel_token
        self.expired = expired
        self.tool_timeout_s = tool_timeout_s
        self.emit = emit
        self.owner_thread = threading.get_ident()

    def _assert_owner(self):
        if threading.get_ident() != self.owner_thread:
            raise RuntimeError("tool_batch_runtime_requires_owner")

    def run(self, calls, *, native_content=None):
        self._assert_owner()
        try:
            batch = ToolCallBatch.create(
                calls,
                run_id=self.progress.run_id,
                turn_id=self.progress.turn_id,
                context=self.context,
                registry=self.registry,
                allowed_tools=self.allowed_tools,
                native_content=native_content,
            )
        except ToolBatchProtocolError as exc:
            self.emit("tool_batch_protocol_error", {"error_code": exc.code})
            raise
        self.hooks.activate(batch)
        flows, operations, refs = {}, {}, {}

        def prepare(call):
            self._assert_owner()
            spec_timeout = float(call.context.registry[call.tool_name].get("timeout_s", 0) or 0)
            limits = [value for value in (self.tool_timeout_s, spec_timeout) if value > 0]
            call.context.timeout_s = min(limits) if limits else 0
            self.hooks.acting(call)
            flow = self.hooks.step_flow(call, call.argument_rejection())
            flows[call.call_id] = flow
            preflight = next(flow)
            if preflight is not None:
                return preflight
            if not batch.parallel:
                return lambda: self.hooks.execute_serial(call)
            if self.hooks.plan_operation is not None:
                operation = self.hooks.plan_operation(call)
                operations[call.call_id] = operation
                try:
                    next(operation)
                except StopIteration as done:
                    operations.pop(call.call_id)
                    return done.value
            # Snapshot executor/history on the owner; workers only run the gated call.
            execute = self.executor.for_call(call.context)

            def work():
                def gated():
                    return execute.execute_gated(call.tool_name, call.arguments)

                return self.hooks.dispatch(call.tool_name, gated)

            return work

        def collect(call, result):
            self._assert_owner()
            if result.status == "uncertain":
                self.hooks.uncertain()
            result = recheck_read(result)
            operation = operations.pop(call.call_id, None)
            if operation is not None:
                try:
                    operation.send(result)
                except StopIteration as done:
                    result = done.value
                else:
                    operation.close()
                    raise RuntimeError("tool operation yielded twice")
            return result

        def settle(call, result):
            self._assert_owner()
            old_status = result.status
            result = recheck_read(result)
            if result.status != old_status:
                scheduler.finish(call, result)
            flow = flows.pop(call.call_id, None)
            if flow is None:
                # Cancelled queued calls still have paired observations, without reservation.
                flow = self.hooks.step_flow(call, result)
                next(flow)
            try:
                settled = self.hooks.settle_step(flow, result)
            finally:
                flow.close()
            call.result_ref = settled.observation_ref
            refs[call.call_id] = settled.observation_ref
            self.session["turn_progress"] = self.progress.checkpoint(batch)
            return {"type": "tool_result", "tool_use_id": call.call_id, "content": settled.content}

        scheduler = ToolBatchScheduler(
            read_permits(self.context.root, self.progress.run_id),
            self.progress,
            on_result=collect,
        )
        try:
            results = scheduler.run(
                batch, prepare, settle, cancel_token=self.cancel_token, expired=self.expired
            )
            batch.status = (
                "uncertain"
                if any(c.status == "uncertain" for c in batch.calls)
                else "cancelled"
                if any(c.status == "cancelled" for c in batch.calls)
                else "completed"
            )
            self.progress.emit("tool_batch_completed", batch_id=batch.batch_id, status=batch.status)
        finally:
            try:
                for flow in [*flows.values(), *operations.values()]:
                    flow.close()
            finally:
                self.session["turn_progress"] = self.progress.checkpoint(batch)
        return results, refs
