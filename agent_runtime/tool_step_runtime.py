"""Owner-side tool steps: preflight, execution, observation and repair feedback."""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent_runtime.cancellation import CancelledError
from agent_runtime.compression_pipeline import truncate_tool_result_for_agent
from agent_runtime.react_phases import ReactPath, ReactPhase
from agent_runtime.step_guard import StepContext
from agent_runtime.stop_reasons import StopReason


@dataclass
class ToolStepState:
    stop_reason: str = ""
    blocked_convergence_reads: int = 0
    action_required: bool = False
    recovery_directive: str = ""
    recovery_allowed_tools: set[str] | None = None
    recovery_kind: str = ""
    in_flight_tool: str = ""
    pending_batch_tools: int = 0
    last_observation_id: str = ""
    no_progress_steps: int = 0


@dataclass(frozen=True)
class ToolStepHooks:
    abort_if_cancelled: Callable
    advance_todo: Callable
    budget_allows_tool: Callable
    budget_reserve: Callable
    emit: Callable
    grant_read_reserve: Callable
    has_targeted_read_reserve: Callable
    matching_read_reservation: Callable
    notify: Callable
    notify_react_phase: Callable
    persist_step_checkpoint: Callable
    record_tool_outcome: Callable


@dataclass(frozen=True)
class ToolStepRuntime:
    agent: Any
    state: ToolStepState
    policy_context: Any
    guard: Any
    budget: Any
    deadline: Any
    recorder: Any
    todos: list[dict]
    hooks: ToolStepHooks
    progress: Any = None
    batch: Any = None


def _log_loop(msg: str) -> None:
    """Loop 阶段 debug 日志（受 --log-level 控制）。"""
    from agent_runtime.logging_setup import get_logger

    get_logger("agent_loop").debug(msg.rstrip("\n"))


def run_tool_step(runtime, ts, tool_name, tool_args, **kwargs):
    flow = tool_step_flow(runtime, ts, tool_name, tool_args, **kwargs)
    try:
        request = next(flow)
    except StopIteration as done:
        return done.value
    try:
        context = kwargs.get("call_context")
        if request is not None:
            result = request
        elif context is not None:
            result = runtime.agent.execute_tool(tool_name, tool_args, call_context=context)
        else:
            result = runtime.agent.execute_tool(tool_name, tool_args)
    except BaseException as exc:
        flow.throw(exc)
        raise
    try:
        flow.send(result)
    except StopIteration as done:
        return done.value
    raise RuntimeError("tool step yielded twice")


def tool_step_flow(
    runtime,
    ts,
    tool_name: str,
    tool_args: dict,
    *,
    step: int,
    path: ReactPath,
    callback=None,
    record_assistant: bool = True,
    emit_acting: bool = True,
    emit_observation: bool = True,
    emit_recording: bool = True,
    call_context=None,
    prepared_result=None,
):
    """执行工具、写 trace/history，返回下一轮 user_message。"""
    if (
        prepared_result is None
        and (msg := runtime.hooks.abort_if_cancelled(ts, phase="pre_tool", in_flight=tool_name))
        is not None
    ):
        raise CancelledError("user", answer=msg)
    from agent_runtime.tool_budget import infer_tool_budget_group

    shared_inflight = call_context is None or not call_context.isolated
    tool_registry = call_context.registry if call_context is not None else runtime.agent.tools

    group = infer_tool_budget_group(tool_name, (tool_registry or {}).get(tool_name))
    idempotency_key = (
        call_context.idempotency_key
        if call_context is not None
        else f"{ts.run_id}:{step}:{tool_name}"
    )
    replayed = prepared_result is not None
    if prepared_result is not None:
        result = prepared_result
    replay_blocked = False
    from agent_runtime.context_runtime import find_action_by_idempotency

    prior_action = (
        None
        if prepared_result is not None
        else find_action_by_idempotency(runtime.agent.session, idempotency_key)
    )
    if prior_action is not None:
        prior_status = str(prior_action.get("status", ""))
        prior_spec = (tool_registry or {}).get(tool_name) or {}
        if prior_status in {"verified", "succeeded"}:
            from agent_runtime.tool_result import ToolResult

            result = ToolResult(
                content="[idempotent replay] 已复用已验证的工具结果",
                status="success",
                metadata={"replayed": True, "observation_id": prior_action.get("result_ref", "")},
                receipt=prior_action.get("receipt", {}),
            )
            replayed = True
        elif prior_status in {"dispatched", "uncertain"} and str(
            prior_spec.get("side_effect", "none")
        ) not in {"read", "none", ""}:
            from agent_runtime.tool_result import ToolResult

            result = ToolResult(
                content="Error: 幂等操作状态不确定，需先执行 postcondition reconciliation",
                status="uncertain",
                error_code="idempotency_conflict",
                metadata={"action": prior_action},
                retryable=False,
            )
            replay_blocked = True
    convergence_blocked = False
    if not replayed and not replay_blocked:
        policy_result = runtime.agent.loop_policy.preflight(
            runtime.policy_context, tool_name, tool_args, step=step,
        )
        if policy_result is not None:
            result = policy_result
            convergence_blocked = True
    budget_rejected = runtime.deadline.expired()
    if not replayed and not replay_blocked and not convergence_blocked and budget_rejected:
        from agent_runtime.tool_result import ToolResult

        result = ToolResult(
            content="Error: repair 全局执行期限已耗尽",
            metadata={},
            status="rejected",
            error_code="deadline_exceeded",
            retryable=False,
        )
    elif (
        not replayed
        and not replay_blocked
        and not convergence_blocked
        and (
            not runtime.budget.allow_tool(group.value)
            or (
                runtime.budget.max_tool_calls > 0
                and runtime.budget.tool_calls + runtime.state.pending_batch_tools
                >= runtime.budget.max_tool_calls
            )
            or not runtime.hooks.budget_allows_tool(group.value)
        )
    ):
        from agent_runtime.tool_result import ToolResult

        result = ToolResult(
            content=f"Error: 工具组 {group.value} 预算已耗尽",
            metadata={"budget_group": group.value},
            status="rejected",
            error_code="budget_exceeded",
            retryable=False,
        )
        budget_rejected = True
    runtime.hooks.notify(
        "on_pre_tool",
        callback,
        step=step,
        tool_name=tool_name,
        tool_args=tool_args,
        path=str(path),
    )
    if emit_acting:
        runtime.hooks.notify_react_phase(
            ReactPhase.ACTING,
            step=step,
            path=path,
            tool=tool_name,
            callback=callback,
        )
    if record_assistant:
        runtime.agent.record(
            {
                "role": "assistant",
                "content": f"调用工具: {tool_name}",
                "tool_name": tool_name,
                "tool_args": tool_args,
            }
        )
    t0 = _time.monotonic()

    def seal_result(result):
        from agent_runtime.tool_executor import _canonical_args_hash
        from agent_runtime.tool_result import attach_tool_receipt

        result.duration_ms = int((_time.monotonic() - t0) * 1000)
        raw_call = runtime.agent.session.get("_last_canonical_tool_call", {}) or {}
        return attach_tool_receipt(
            result,
            tool_name,
            args_hash=_canonical_args_hash(tool_name, tool_args),
            run_id=call_context.run_id if call_context is not None else ts.run_id,
            call_id=call_context.call_id
            if call_context is not None
            else raw_call.get("call_id", ""),
        )

    if not budget_rejected and not replayed and not replay_blocked and not convergence_blocked:
        runtime.hooks.budget_reserve("tool_calls")
        if group.value in {"write", "verify", "recovery"}:
            runtime.hooks.budget_reserve(
                {"write": "writes", "verify": "verifies", "recovery": "recoveries"}[group.value]
            )
        if call_context is not None:
            call_context.budget_reserved = True
        if shared_inflight:
            runtime.state.in_flight_tool = tool_name
        else:
            runtime.state.pending_batch_tools = runtime.state.pending_batch_tools + 1
        from agent_runtime.context_runtime import build_action_record, transition_action

        tool_spec = (tool_registry or {}).get(tool_name) or {}
        action = build_action_record(
            tool_name,
            tool_args,
            revision=int(runtime.agent.session.get("state_revision", 0) or 0),
            side_effect=str(tool_spec.get("side_effect", "none") or "none"),
            idempotency_key=idempotency_key,
            status="dispatched",
        )
        action_raw = action.__dict__.copy()
        if shared_inflight:
            runtime.agent.session["_in_flight_action"] = action_raw
        try:
            result = yield
            runtime.agent.loop_policy.review_result(
                runtime.policy_context, tool_name, tool_args, result
            )
            result = seal_result(result)
        except BaseException:
            action_raw["status"] = "uncertain"
            action_raw["uncertain_reason"] = "runtime_exception"
            if call_context is not None:
                runtime.agent.session.setdefault("action_ledger", []).append(action_raw)
            raise
        else:
            result_meta = result.to_metadata()
            result_status = str(result.status)
            error_code = str(result.error_code)
            if result_status == "success":
                next_status = (
                    "verified"
                    if str(tool_spec.get("side_effect", "none")) in {"read", "none"}
                    or result_meta.get("postcondition_verified")
                    else "acknowledged"
                )
            elif result_status in {"cancelled", "uncertain"} or error_code in {
                "tool_timeout",
                "deadline_exceeded",
                "tool_cancelled",
            }:
                next_status = "uncertain"
            else:
                next_status = "failed"
            try:
                action_raw = transition_action(
                    action_raw,
                    next_status,
                    reason=error_code,
                    receipt=result.receipt,
                )
            except ValueError:
                action_raw["status"] = "uncertain"
                action_raw["uncertain_reason"] = "invalid_transition"
            action_raw["result_ref"] = str(result_meta.get("observation_id", ""))
            runtime.agent.session.setdefault("action_ledger", []).append(action_raw)
            runtime.agent.session["action_ledger"] = runtime.agent.session["action_ledger"][-100:]
            if shared_inflight:
                runtime.agent.session.pop("_in_flight_action", None)
        finally:
            if shared_inflight:
                runtime.state.in_flight_tool = ""
            else:
                runtime.state.pending_batch_tools -= 1
    if budget_rejected or replayed or replay_blocked or convergence_blocked:
        result = yield result
        result = seal_result(result)
    # Gateway/权限拒绝不计入 tool_steps，避免无效步耗尽预算（E5）
    _meta = result.metadata
    if result.status != "rejected" and prepared_result is None:
        ts.record_tool(tool_name)
        runtime.budget.record_tool(group.value)
    else:
        ts.last_tool = tool_name
    if (
        call_context is None
        and (msg := runtime.hooks.abort_if_cancelled(ts, phase="post_tool", in_flight=tool_name))
        is not None
    ):
        raise CancelledError("user", answer=msg)
    result_text = result.content
    te_ms = result.duration_ms
    from agent_runtime.repair_runtime import CanonicalToolCall

    raw_call = runtime.agent.session.get("_last_canonical_tool_call", {})
    canonical_call = CanonicalToolCall.create(
        tool_name,
        tool_args,
        source=raw_call.get("source", "native"),
        call_id=(call_context.call_id if call_context is not None else raw_call.get("call_id", "")),
    )
    if (
        call_context is not None
        and call_context.isolated
        and tool_name == "read_file"
        and result.ok
    ):
        from agent_runtime.tools import _mark_edit_lock_read

        _mark_edit_lock_read(runtime.agent.tool_context, str(tool_args.get("path", "")))
    recorded = runtime.recorder.record(
        canonical_call,
        result,
        duration_ms=te_ms,
        metadata=_meta,
        source_version=((tool_registry or {}).get(tool_name) or {}).get("version", ""),
        idempotency_key=idempotency_key,
        call_context=call_context,
    )
    stored = recorded.stored
    runtime.state.last_observation_id = stored.observation_id
    retrieval = _meta.get("retrieval_result")
    runtime.agent.loop_policy.on_result(
        runtime.policy_context, tool_name, tool_args, result, step=step,
    )
    result_text = runtime.agent.loop_policy.feedback(runtime.policy_context, tool_name, result)
    runtime.hooks.notify(
        "on_post_tool",
        callback,
        step=step,
        tool_name=tool_name,
        result_preview=result_text[:200],
        elapsed_ms=te_ms,
        path=str(path),
    )
    projection_input = result_text
    if isinstance(retrieval, dict):
        from agent_runtime.code_exploration.consumption import retrieval_header

        projection_input = retrieval_header(retrieval) + result_text
    projected_result_text = truncate_tool_result_for_agent(
        runtime.agent, tool_name, projection_input
    )
    if len(projected_result_text) < len(projection_input):
        result.output_truncated = True
        if stored.raw_ref:
            projected_result_text += (
                f"\n[output_truncated=true artifact_ref={stored.raw_ref} "
                f"observation_id={stored.observation_id}]"
            )
    result_text = projected_result_text
    ts.node_timings.setdefault("tool_exec_ms", 0)
    ts.node_timings["tool_exec_ms"] += te_ms
    _log_loop(f"  [loop] {tool_name} tool={te_ms}ms\n")
    if emit_observation:
        runtime.hooks.notify_react_phase(
            ReactPhase.OBSERVATION,
            step=step,
            path=path,
            tool=tool_name,
            callback=callback,
        )
    if result.status == "success":
        runtime.agent.update_memory_after_tool(tool_name, tool_args, result_text)
    runtime.hooks.record_tool_outcome(tool_name, result, ts, tool_args)
    if emit_recording:
        runtime.hooks.notify_react_phase(
            ReactPhase.RECORDING,
            step=step,
            path=path,
            tool=tool_name,
            callback=callback,
        )
    tool_status = "DRY" if result.status == "dry_run" else "OK" if result.ok else "FAIL"
    runtime.hooks.notify(
        "on_tool_executed",
        callback,
        step=step,
        name=tool_name,
        result_preview=result_text,
        elapsed_ms=te_ms,
        status=tool_status,
    )
    return record_step_progress(
        runtime,
        ts,
        tool_name,
        tool_args,
        result,
        result_text,
        stored,
        step=step,
        path=path,
        convergence_blocked=convergence_blocked,
        tool_registry=tool_registry,
    )


def record_step_progress(
    runtime,
    ts,
    tool_name,
    tool_args,
    result,
    result_text,
    stored,
    *,
    step,
    path,
    convergence_blocked,
    tool_registry,
):
    """Accept terminal tools, update progress and persist a successful safe point."""
    # 终态工具：成功后结束 loop，payload 作为 final answer
    tool_spec = (tool_registry or {}).get(tool_name) or {}
    if tool_spec.get("terminal") and result.status == "success":
        from agent_runtime.terminal_tool import TerminalToolAcceptedError

        raise TerminalToolAcceptedError(str(result_text), tool_name=tool_name)
    # 死循环检测：Gate 5.5 rejection → 升级为 stop
    error_code = result.error_code
    if error_code == "loop_detected":
        from agent_runtime.tool_executor import _canonical_args_hash

        runtime.hooks.emit(
            "loop_detected",
            {
                "tool": tool_name,
                "args_hash": _canonical_args_hash(tool_name, tool_args),
                "window_size": int(getattr(runtime.agent.config, "loop_detect_threshold", 3) or 3),
            },
        )
        ts.stop_with_reason(
            StopReason.CIRCUIT_BREAKER,
            "stopped",
            detail=f"死循环检测: {tool_name} 连续高频调用",
        )
        runtime.state.stop_reason = StopReason.CIRCUIT_BREAKER
        return ts.final_answer or f"任务因死循环检测终止（{tool_name}）。"

    # 每 tool 步 checkpoint（成功时），供 --resume 从最后成功步继续
    tool_success = result.status == "success"
    if tool_success:
        runtime.hooks.advance_todo()
        runtime.agent.loop_policy.on_success(
            runtime.policy_context, tool_name, tool_args, result, step=step,
        )
    runtime.state.no_progress_steps = runtime.guard.stall_count
    guard_has_affected = runtime.agent.loop_policy.has_progress(
        runtime.policy_context, tool_name, result,
    )
    verdict = None
    if not convergence_blocked:
        verdict = runtime.guard.evaluate(
            StepContext(
                tool_name=tool_name,
                tool_args=tool_args,
                has_affected=guard_has_affected,
                progress_key=(
                    runtime.guard.read_progress_key(tool_name, tool_args) if tool_success else ""
                ),
            )
        )
    if verdict is not None:
        if verdict.reason:
            # 终止级判决
            runtime.hooks.emit(
                "stall_detected" if verdict.reason == StopReason.STALL else "goal_drift",
                {
                    "reason": verdict.reason,
                    "detail": verdict.detail,
                    "steps": runtime.guard.stall_count,
                    "drift_steps": runtime.guard.drift_count,
                },
            )
            for todo in runtime.todos:
                if todo.get("status") == "in_progress":
                    todo["status"] = "blocked"
                    runtime.hooks.emit("todo_updated", {"todo": dict(todo)})
                    break
            # stall 不终止：注入 replan 提示让模型自行调整
            if verdict.reason == StopReason.STALL:
                hint = (
                    f"\n\n⚠ 进展停滞（连续 {runtime.guard.stall_count} 步无文件变更）。"
                    "请检查当前 todo 列表，考虑重新规划或尝试不同策略。"
                )
                hint += runtime.agent.loop_policy.stall_hint
                result_text = result_text + hint
                runtime.agent.record(
                    {
                        "role": "tool",
                        "content": (f"[{stored.observation_id}] {result_text[:800]}"),
                        "tool_name": tool_name,
                        "observation_id": stored.observation_id,
                    }
                )
                next_message = f"工具 {tool_name} 执行完成。\n结果:\n{result_text}"
                runtime.hooks.persist_step_checkpoint(
                    ts,
                    tool_name,
                    tool_args,
                    result_text,
                    next_message,
                    result,
                    step=step,
                    path=path,
                )
                return next_message
            # goal_drift 仍终止
            ts.stop_with_reason(verdict.reason, "stopped", detail=verdict.detail)
            runtime.state.stop_reason = verdict.reason
            return verdict.replan_hint or f"任务终止：{verdict.detail}"
        else:
            if verdict.action == "enter_convergence":
                runtime.agent.loop_policy.enter_convergence(
                    runtime.policy_context,
                    runtime.guard.convergence_reason or "read_limit_without_write",
                    step=step,
                )
                result_text += f"\n\n{verdict.replan_hint}"
            else:
                # warning 级（drift 预警，不终止）
                runtime.hooks.emit("goal_drift_warning", {"detail": verdict.detail})
    runtime.agent.record(
        {
            "role": "tool",
            "content": f"[{stored.observation_id}] {result_text[:800]}",
            "tool_name": tool_name,
            "observation_id": stored.observation_id,
        }
    )
    next_message = f"工具 {tool_name} 执行完成。\n结果:\n{result_text}"
    progress = runtime.progress
    if progress is not None:
        runtime.agent.session["turn_progress"] = progress.checkpoint(runtime.batch)
    runtime.hooks.persist_step_checkpoint(
        ts,
        tool_name,
        tool_args,
        result_text,
        next_message,
        result,
        step=step,
        path=path,
    )
    return next_message
