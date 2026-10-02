"""Native and text model turns with explicit state and owner callbacks."""

from __future__ import annotations

import json
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.cancellation import CancelledError
from agent_runtime.context_metadata import build_trace_payload
from agent_runtime.errors import ContextBuildBlockedError, EmptyModelResponse
from agent_runtime.model_timing import collect_client_timings
from agent_runtime.parse_recovery import (
    ParseRetry,
    build_recovery_prompt,
    failure_invalid_tool_payload,
)
from agent_runtime.react_phases import ReactPhase
from agent_runtime.stop_reasons import StopReason
from agent_runtime.tool_step_runtime import ToolStepState

MAX_EMPTY_RETRIES = 3
MAX_JSON_RETRIES = 2


@dataclass
class ProtocolState:
    last_token_meta: dict = field(default_factory=dict)
    last_budget_meta: dict = field(default_factory=dict)
    prepared_context_manager: Any = None
    prepared_request: Any = None
    retry_count: int = 0
    json_retry_count: int = 0
    empty_retries: int = 0
    llm_call_count: int = 0


@dataclass(frozen=True)
class ProtocolHooks:
    abort_if_cancelled: Callable
    accumulate_context_stats: Callable
    apply_call_usage_meta: Callable
    begin_edit_lock_turn: Callable
    budget_payload: Callable
    budget_reserve: Callable
    complete_run: Callable
    emit: Callable
    emit_stream_event: Callable
    get_task_summary_text: Callable
    invoke_model_call: Callable
    maybe_step_timeout: Callable
    native_tool_names: Callable
    notify: Callable
    notify_react_phase: Callable
    record_model_timings: Callable
    sleep_with_deadline: Callable
    cancel_token: Callable


@dataclass(frozen=True)
class ProtocolRuntime:
    agent: Any
    state: ProtocolState
    control: ToolStepState
    guard: Any
    hooks: ProtocolHooks
    stream_enabled: bool = False


def _log_loop(msg: str) -> None:
    """Loop 阶段 debug 日志（受 --log-level 控制）。"""
    from agent_runtime.logging_setup import get_logger

    get_logger("agent_loop").debug(msg.rstrip("\n"))


def patch_recovery_anchors(text: str, *, max_chars: int = 6000) -> str:
    """Keep bounded source/target anchors when a model turn is truncated."""
    raw = str(text or "")
    if not raw:
        return ""
    lines = raw.splitlines()
    markers = (
        "allowed_edit:",
        "DISK GROUNDING",
        "嫌疑位置",
        "相关测试文件",
        "失败面",
        "[PATCHER RUNTIME CONTRACT]",
    )
    starts = [
        index for index, line in enumerate(lines) if any(marker in line for marker in markers)
    ]
    chunks: list[str] = []
    used = 0
    for start in starts:
        chunk = "\n".join(lines[start : start + 36]).strip()
        if not chunk:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(chunk) > remaining:
            chunk = chunk[:remaining].rstrip() + "\n... anchors truncated ..."
        chunks.append(chunk)
        used += len(chunk)
    return "\n\n".join(chunks)


def build_native_tools(
    tools_registry: dict, *, allowed_names: set[str] | None = None
) -> list[dict]:
    """将内部工具注册表转换为 Anthropic tool_use 格式（仅 schema 字段）。"""
    from agent_runtime.tool_schema import schema_to_json, tool_schema_view

    result = []
    for name, spec in tool_schema_view(tools_registry).items():
        if allowed_names is not None and name not in allowed_names:
            continue
        schema = spec.get("json_schema") or spec.get("schema", {})
        result.append(
            {
                "name": name,
                "description": spec.get("description", ""),
                "input_schema": schema_to_json(schema),
            }
        )
    return result


def check_hard_cap(config, token_meta: dict) -> str | None:
    """Prompt 超出 configured hard cap 时返回错误消息。"""
    total = token_meta.get("total_tokens", 0) or token_meta.get("context_sections_total", 0)
    hard_cap = int(getattr(config, "hard_cap", 8_000) or 8_000)
    if total > hard_cap:
        return (
            f"<final>Prompt 大小 {total} tokens 超出硬顶限制 ({hard_cap})。"
            "请缩短输入或使用 /reset 清空对话历史后重试。</final>"
        )
    return None


def validate_final_answer(config, text: str) -> tuple[bool, str]:
    """校验 final answer 的 JSON 语法与可选 schema。

    Returns:
        (ok, error_message)。ok=True 表示通过，error_message 为 recovery 提示。
    """
    if not config.json_mode:
        return True, ""
    schema = config.final_schema

    # L1: JSON 语法
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return False, (
            f"上一轮 final answer 不是合法 JSON（{e}）。"
            "请严格输出 JSON 格式的最终答案，不要包裹在 markdown 代码块中。"
        )

    # L2: Schema 字段校验
    if schema:
        missing = [f for f in schema if f not in data]
        if missing:
            return False, (
                f"上一轮 final answer 缺少必填字段: {missing}。"
                f"请输出包含 {list(schema.keys())} 的完整 JSON。"
            )
        type_map = {
            "str": str,
            "int": int,
            "float": (int, float),
            "bool": bool,
            "list": list,
            "dict": dict,
        }
        for field, ftype in schema.items():
            expected = type_map.get(ftype)
            if expected is None:
                continue
            value = data.get(field)
            if value is not None and not isinstance(value, expected):
                return False, (
                    f"字段 '{field}' 应为 {ftype} 类型，实际为 {type(value).__name__}。"
                    "请修正后重新输出。"
                )

    return True, ""


def strip_cot(raw: str) -> str:
    """剥离模型输出中的思考内容（CoT），返回清洗后的文本。

    两步：
    1. 移除 ``<think>...</think>`` 标签块（DeepSeek-R1 / OpenAI o1）
    2. 移除第一个 ``<tool>`` 或 ``<final>`` 标签前的自然语言前缀

    若清洗后为空，返回原始文本（安全回退）。
    """
    import re

    # Step 1: 移除显式 <think> 标签
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)

    # Step 2: 移除第一个结构化标签前的自然语言前缀
    m = re.search(r"<(tool|final)>", cleaned)
    if m and m.start() > 0:
        prefix = cleaned[: m.start()].strip()
        if prefix:
            cleaned = cleaned[m.start() :]

    cleaned = cleaned.strip()
    return cleaned if cleaned else raw.strip()


def last_successful_tool_call(agent) -> dict | None:
    """从 session history 中找上一次成功的 tool 调用。"""
    history = agent.session.get("history", [])
    for h in reversed(history):
        if h.get("role") == "tool" and h.get("tool_name"):
            return {"name": h["tool_name"], "args": h.get("tool_args", {})}
    return None


def xml_build_context(runtime, ts, user_message: str, *, step: int, callback) -> str:
    t0 = _time.time()
    # XML continuations (including old checkpoints) carry raw tool output.
    # Reference code observations instead, so the current history projection
    # is the only source body used after freshness checks.
    last = runtime.agent.session.get("_last_tool_observation", {})
    oid = str(last.get("observation_id", ""))
    record = runtime.agent.session.get("observations", {}).get(oid, {})
    prefix = f"工具 {record.get('tool', '')} 执行完成。\n结果:\n"
    if record.get("retrieval_result") and user_message.startswith(prefix):
        user_message = prefix + f"[observation_ref={oid}; see validated context history]"
    from agent_runtime.context_manager import ContextManager

    manager = ContextManager(runtime.agent)
    request, token_meta = manager.prepare_request(user_message, protocol="xml")
    prompt_text = request.messages[0]["content"]
    runtime.state.prepared_context_manager = manager
    runtime.state.prepared_request = request
    if hard_limit := check_hard_cap(runtime.agent.config, token_meta):
        return hard_limit
    if token_meta.get("required_state_ref"):
        try:
            runtime.agent._plan_session.validate_required_context(token_meta["long_task_context"])
        except ValueError:
            raise ContextBuildBlockedError("state_mismatch") from None
    from agent_runtime.message_projection import (
        attach_projection_metadata,
        build_context_prefix,
    )

    context_prefix = build_context_prefix(runtime.agent, token_meta)
    attach_projection_metadata(token_meta, runtime.agent.session, context_prefix=context_prefix)
    runtime.state.last_token_meta = token_meta
    if not runtime.hooks.budget_reserve("prompt_tokens", token_meta.get("total_tokens", 0)):
        return "<final>Prompt token 预算已耗尽。</final>"
    runtime.hooks.accumulate_context_stats(token_meta)
    runtime.hooks.emit("context_built", build_trace_payload(token_meta))
    runtime.hooks.begin_edit_lock_turn()
    runtime.hooks.notify_react_phase(
        ReactPhase.REASONING,
        step=step,
        path="xml",
        callback=callback,
    )
    ts.node_timings.setdefault("prompt_build_ms", 0)
    ts.node_timings["prompt_build_ms"] += int((_time.time() - t0) * 1000)
    return prompt_text


def xml_call_model(runtime, ts, prompt_text: str, *, step: int, callback=None) -> tuple[str, float]:
    # LLM 调用预算硬顶
    max_calls = runtime.agent.config.budget.max_llm_calls
    if max_calls > 0 and runtime.state.llm_call_count >= max_calls:
        ts.stop_with_reason(
            StopReason.BUDGET_EXHAUSTED, "stopped", detail=f"max_llm_calls={max_calls}"
        )
        runtime.control.stop_reason = StopReason.BUDGET_EXHAUSTED
        raise CancelledError(
            "budget", answer=(f"<final>LLM 调用达到硬顶 ({max_calls})，任务终止。</final>")
        )
    if not runtime.hooks.budget_reserve("llm_calls"):
        ts.stop_with_reason(
            StopReason.BUDGET_EXHAUSTED,
            "stopped",
            detail="unified budget llm_calls exhausted",
        )
        runtime.control.stop_reason = StopReason.BUDGET_EXHAUSTED
        raise CancelledError("budget", answer="<final>统一预算已耗尽，任务终止。</final>")
    runtime.state.llm_call_count += 1
    ts.record_attempt()
    t1 = _time.time()
    runtime.hooks.emit(
        "model_request_start",
        {
            "step": ts.tool_steps + 1,
            "attempt": ts.attempts,
            "model": getattr(runtime.agent.config, "model", ""),
            "runtime_budget": runtime.hooks.budget_payload(),
        },
    )
    meta = runtime.state.last_token_meta or {}
    cache_key = str(meta.get("prompt_cache_key", "") or "")
    effective_output_tokens = int(
        (runtime.agent.session.get("runtime_degradation") or {}).get(
            "max_output_tokens",
            getattr(runtime.agent.config, "max_new_tokens", 4096),
        )
        or 4096
    )
    for empty_try in range(MAX_EMPTY_RETRIES):
        try:
            if meta.get("request_hash"):
                runtime.state.prepared_context_manager.validate_prepared_request(
                    runtime.state.prepared_request, meta
                )
            if runtime.stream_enabled and hasattr(runtime.agent.model_client, "complete_stream"):

                def on_chunk(chunk: str) -> None:
                    runtime.hooks.emit_stream_event(
                        "token_delta", {"chars": len(chunk)}, phase="model", turn=step
                    )
                    if callback is not None and hasattr(callback, "on_chunk"):
                        callback.on_chunk(chunk)

                raw = runtime.hooks.invoke_model_call(
                    lambda: runtime.agent.circuit_breaker.call(
                        runtime.agent.model_client.complete_stream,
                        prompt_text,
                        max_new_tokens=effective_output_tokens,
                        on_chunk=on_chunk,
                        cancel_token=runtime.hooks.cancel_token(),
                    )
                )
            else:
                try:
                    raw = runtime.hooks.invoke_model_call(
                        lambda: runtime.agent.circuit_breaker.call(
                            runtime.agent.model_client.complete,
                            prompt_text,
                            max_new_tokens=effective_output_tokens,
                            prompt_cache_key=cache_key,
                        )
                    )
                except TypeError as exc:
                    if "prompt_cache_key" not in str(exc):
                        raise
                    raw = runtime.hooks.invoke_model_call(
                        lambda: runtime.agent.circuit_breaker.call(
                            runtime.agent.model_client.complete,
                            prompt_text,
                            max_new_tokens=effective_output_tokens,
                        )
                    )
            break  # 成功，退出重试循环
        except EmptyModelResponse:
            runtime.state.empty_retries += 1
            runtime.hooks.emit(
                "empty_model_response",
                {
                    "attempt": empty_try + 1,
                    "step": step,
                },
            )
            if empty_try < MAX_EMPTY_RETRIES - 1:
                if not runtime.hooks.sleep_with_deadline(0.5 * (empty_try + 1)):
                    raise CancelledError(
                        "deadline",
                        answer="<final>重试退避期间已达到全局 deadline。</final>",
                    )
            else:
                ts.stop_with_reason(
                    StopReason.API_ERROR,
                    "stopped",
                    detail="empty_model_response_exhausted",
                )
                runtime.control.stop_reason = StopReason.API_ERROR
                raise CancelledError(
                    "api_error",
                    answer=(
                        "<final>API 错误: 模型连续返回空响应，已重试 "
                        f"{MAX_EMPTY_RETRIES} 次。</final>"
                    ),
                )

    ts.node_timings.setdefault("model_call_ms", 0)
    ts.node_timings["model_call_ms"] += int((_time.time() - t1) * 1000)
    runtime.hooks.record_model_timings(
        ts,
        collect_client_timings(runtime.agent.model_client),
        default_attempt=ts.attempts,
    )
    return raw, t1


def handle_parse_retry(runtime, ts, raw: str, payload, *, step: int) -> str:
    runtime.state.retry_count += 1
    delay = min(2 ** (runtime.state.retry_count - 1), 8)
    _log_loop(
        f"  [loop] retry#{runtime.state.retry_count} backoff={delay}s "
        f"raw[:100]={raw.strip()[:100]}\n"
    )
    try:
        from pathlib import Path

        dbg = Path(runtime.agent._cwd) / ".agent" / "debug_retry.txt"
        dbg.parent.mkdir(parents=True, exist_ok=True)
        with open(dbg, "a", encoding="utf-8") as f:
            f.write(f"\n=== retry#{runtime.state.retry_count} ===\n{raw}\n")
    except Exception:
        pass
    if not runtime.hooks.sleep_with_deadline(delay):
        raise CancelledError(
            "deadline",
            answer="<final>解析重试退避期间已达到全局 deadline。</final>",
        )
    prompt = str(payload)
    failure = payload.failure if isinstance(payload, ParseRetry) else None
    if failure is not None:
        runtime.hooks.emit(
            "parse_retry",
            {
                "kind": failure.kind,
                "attempt": runtime.state.retry_count,
                "snippet_len": len(failure.snippet),
                "error_offset": failure.error_offset,
            },
        )
    runtime.agent.record({"role": "system", "content": prompt})
    return prompt


def xml_invalid_tool_retry(runtime, ts, payload, *, raw: str, step: int) -> str:
    failure = failure_invalid_tool_payload(payload)
    last = last_successful_tool_call(runtime.agent)
    prompt = build_recovery_prompt(failure, last_tool_call=last)
    retry = ParseRetry(prompt, failure, has_last_tool_anchor=last is not None)
    return handle_parse_retry(runtime, ts, raw, retry, step=step)


def handle_native_output(
    runtime, ts, result, *, turn: int, max_output: int, recovery_attempt: int
) -> tuple[str, str | None]:
    """Return a recovery directive or terminal answer; ordinary output returns neither.

    Empty and truncated output share one recovery allowance for the run.
    Partial content is traced by size only and never committed to history.
    """
    from agent_runtime.model_turn import FinishKind

    finish = result.finish
    if finish.kind == FinishKind.CONTENT_FILTER:
        ts.stop_with_reason(
            StopReason.API_ERROR, "failed", detail=f"content_filter:{finish.raw_reason}"
        )
        return "", runtime.hooks.complete_run(
            ts, "<final>模型输出被 Provider 安全策略拦截。</final>"
        )
    if finish.kind not in {FinishKind.MAX_OUTPUT_TOKENS, FinishKind.EMPTY_OUTPUT}:
        return "", None

    truncated = finish.kind == FinishKind.MAX_OUTPUT_TOKENS
    if truncated:
        content_blocks = result.content if isinstance(result.content, list) else []
        block_counts: dict[str, int] = {}
        for block in content_blocks:
            block_type = (
                str(block.get("type") or "unknown") if isinstance(block, dict) else "invalid"
            )
            block_counts[block_type] = block_counts.get(block_type, 0) + 1
        if (
            content_blocks
            and set(block_counts) <= {"thinking"}
            and not result.text
            and not result.tool_calls
        ):
            runtime.control.patch_decision_required = True
            runtime.guard.enter_convergence("thinking_only_truncation")
            runtime.hooks.emit(
                "thinking_only_truncation",
                {
                    "step": turn,
                    "requested_max_output_tokens": max_output,
                    "actual_output_tokens": int(result.usage.get("output_tokens", 0) or 0),
                    "recovery_attempt": recovery_attempt + 1,
                },
            )
        runtime.hooks.emit(
            "model_output_truncated",
            {
                "step": turn,
                "requested_max_output_tokens": max_output,
                "actual_output_tokens": int(result.usage.get("output_tokens", 0) or 0),
                "text_chars": len(result.text or ""),
                "content_block_count": len(content_blocks),
                "content_block_counts": block_counts,
                "tool_call_count": len(result.tool_calls),
                "recovery_attempt": recovery_attempt + 1,
                "history_action": "discarded",
            },
        )

    if recovery_attempt < 1:
        directive = (
            "[OUTPUT RECOVERY] The previous model output was truncated and was "
            "discarded. Do not continue or repeat that analysis. Complete this turn "
            "with exactly one apply_patch/patch_file call, or call finish_repair with "
            "a grounded cannot_patch/needs_more_context reason. "
            "Do not perform more broad exploration."
            if truncated
            else "[EMPTY OUTPUT RECOVERY] The previous response was empty. Produce exactly "
            "one apply_patch/patch_file call, or call finish_repair with a grounded "
            "cannot_patch/needs_more_context reason."
        )
        return directive, None

    if truncated:
        reason = StopReason.MODEL_OUTPUT_TRUNCATED
        detail = (
            f"provider_finish={finish.kind.value}; "
            f"requested={max_output}; recovery_attempts={recovery_attempt}"
        )
        answer = (
            '<final>{"status":"needs_more_context",'
            '"reason":"模型输出连续被截断，未执行不完整内容。"}</final>'
        )
    else:
        reason = StopReason.PARSE_FAIL
        detail = f"provider_finish={finish.kind.value}"
        answer = f"<final>模型输出无效：{finish.kind.value}</final>"
    ts.stop_with_reason(reason, "failed", detail=detail)
    return "", runtime.hooks.complete_run(ts, answer)


def xml_final_retry(runtime, ts, raw: str, answer: str, *, step: int) -> str | None:
    """Validate XML finals and reuse the existing bounded parse recovery path."""
    from agent_runtime.parse_recovery import ParseFailure, make_parse_retry

    ok, error = validate_final_answer(runtime.agent.config, answer)
    limit = getattr(runtime.agent.config, "max_json_retries", MAX_JSON_RETRIES)
    if ok or runtime.state.json_retry_count >= limit:
        runtime.state.json_retry_count = 0
        return None
    runtime.state.json_retry_count += 1
    runtime.hooks.emit("json_retry", {"attempt": runtime.state.json_retry_count, "error": error})
    failure = ParseFailure(
        kind="json_in_tool",
        snippet=answer[:500],
        error_offset=None,
        error_message=error,
        hint="final answer JSON 格式错误",
    )
    return handle_parse_retry(runtime, ts, raw, make_parse_retry(raw, failure), step=step)


def native_model_turn(
    runtime,
    ts,
    user_message: str,
    *,
    turn: int,
    step_clock,
    latency_decision,
    native_tail,
    native_tail_refs,
    usage_total,
    output_recovery_directive: str,
    output_recovery: int,
    callback=None,
):
    """Build native messages, call once and normalize the response for the shared loop."""
    from agent_runtime.context_manager import ContextManager
    from agent_runtime.message_projection import (
        attach_projection_metadata,
        build_context_prefix,
    )
    from agent_runtime.model_turn import FinishKind, ToolChoice, ToolChoiceMode
    from agent_runtime.response import CanonicalResponse

    client = runtime.agent.model_client
    action_required = bool(output_recovery_directive or runtime.control.patch_decision_required)
    tools_def = build_native_tools(
        runtime.agent.tools,
        allowed_names=runtime.hooks.native_tool_names(
            action_required=action_required,
            patch_only_recovery=bool(output_recovery_directive),
        ),
    )
    phase_output_cap = 4096 if not action_required or output_recovery_directive else 2048
    max_output = max(512, min(latency_decision["max_output_tokens"], phase_output_cap, 8192))
    directives = []
    user_override = None
    if output_recovery_directive:
        if getattr(runtime.agent, "_plan_session", None) is not None:
            directives.append(output_recovery_directive)
        else:
            # Preserve the established standalone L1 recovery envelope.
            summary = (
                runtime.hooks.get_task_summary_text().strip() or ts.user_request or user_message
            )
            user_override = output_recovery_directive
            if summary:
                user_override += f"\n[REPAIR TASK]\n{summary[:2000]}"
            anchors = patch_recovery_anchors(user_message)
            if anchors:
                user_override += f"\n[PATCHER EVIDENCE ANCHORS]\n{anchors}"
    elif runtime.control.patch_decision_required:
        directives.append(
            "[PATCH DECISION REQUIRED] Exploration is closed. "
            "Call apply_patch/patch_file now, or call finish_repair with a concise "
            "cannot_patch/needs_more_context reason grounded in the evidence ledger."
        )
    if runtime.control.patch_recovery_directive:
        directives.append("[PATCH RECOVERY]\n" + runtime.control.patch_recovery_directive)
    if user_override and directives:
        user_override += "\n\n" + "\n\n".join(directives)
        directives = []
    manager = ContextManager(runtime.agent)
    request, budget_meta = manager.prepare_request(
        user_message,
        protocol="native",
        tools=tools_def,
        native_tail=native_tail,
        tail_refs=native_tail_refs,
        directives=directives,
        action_required=action_required,
        tool_choice=ToolChoice(ToolChoiceMode.REQUIRED) if action_required else None,
        max_output_tokens=max_output,
        deadline=getattr(step_clock, "_deadline", None),
        user_override=user_override,
    )
    dynamic_user = request.messages[0]["content"]
    runtime.state.prepared_context_manager = manager
    runtime.state.prepared_request = request
    context_prefix = build_context_prefix(runtime.agent, budget_meta)
    attach_projection_metadata(budget_meta, runtime.agent.session, context_prefix=context_prefix)
    runtime.state.last_budget_meta = budget_meta
    budget_meta["runtime_budget"] = runtime.hooks.budget_payload()
    if not runtime.hooks.budget_reserve("prompt_tokens", budget_meta["provider_input_tokens"]):
        ts.stop_with_reason(
            StopReason.CONTEXT_OVERFLOW, "stopped", detail="prompt token budget exhausted"
        )
        return CanonicalResponse.create(
            "final",
            "stop",
            {"text": runtime.hooks.complete_run(ts, "<final>Prompt token 预算已耗尽。</final>")},
        )
    runtime.hooks.accumulate_context_stats(budget_meta)
    runtime.hooks.emit("context_built", build_trace_payload(budget_meta))
    if isinstance(budget_meta.get("emergency_compaction"), dict):
        runtime.hooks.emit(
            "context_emergency_compacted",
            {"step": turn, **budget_meta["emergency_compaction"]},
        )
    if not runtime.hooks.budget_reserve("llm_calls"):
        ts.stop_with_reason(
            StopReason.BUDGET_EXHAUSTED,
            "stopped",
            detail="unified budget llm_calls exhausted",
        )
        return CanonicalResponse.create(
            "final",
            "stop",
            {"text": runtime.hooks.complete_run(ts, "<final>统一 LLM 调用预算已耗尽。</final>")},
        )
    runtime.hooks.emit(
        "model_request_start",
        {
            "step": turn,
            "attempt": turn,
            "model": getattr(runtime.agent.config, "model", ""),
            "runtime_budget": runtime.hooks.budget_payload(),
        },
    )
    runtime.hooks.notify(
        "on_pre_model",
        callback,
        step=turn,
        prompt_preview=dynamic_user[:200],
        path="native",
    )
    call_started = _time.time()
    manager.validate_prepared_request(request, budget_meta)
    result = runtime.hooks.invoke_model_call(lambda: client.complete_turn(request))
    step_clock.check(step=turn, path="native")

    ts.record_attempt()
    elapsed_ms = int((_time.time() - call_started) * 1000)
    for key in usage_total:
        if key == "calls":
            continue
        usage_total[key] += int(result.usage.get(key, 0) or 0)
    usage_total["calls"] += 1
    runtime.hooks.apply_call_usage_meta(usage_total)
    runtime.hooks.record_model_timings(ts, collect_client_timings(client), default_attempt=turn)
    ts.node_timings["model_call_ms"] = (
        int(ts.node_timings.get("model_call_ms", 0) or 0) + elapsed_ms
    )
    finish = result.finish
    ts.node_timings["provider_finish_kind"] = finish.kind.value
    ts.node_timings["provider_finish_reason"] = finish.raw_reason
    runtime.hooks.emit(
        "provider_finish",
        {
            "step": turn,
            "kind": finish.kind.value,
            "raw_reason": finish.raw_reason,
            "provider": finish.provider,
        },
    )
    runtime.hooks.notify(
        "on_post_model",
        callback,
        step=turn,
        raw_preview=result.text[:200],
        elapsed_ms=elapsed_ms,
        path="native",
    )

    if finish.kind == FinishKind.TOOL_CALLS and result.tool_calls:
        assistant_content = result.content or [
            {"type": "tool_use", "id": call.call_id, "name": call.name, "input": call.arguments}
            for call in result.tool_calls
        ]
        return CanonicalResponse.create(
            "tool_call",
            "success",
            {
                "calls": result.tool_calls,
                "content": result.content,
                "assistant_content": assistant_content,
            },
        )
    directive, terminal_answer = handle_native_output(
        runtime, ts, result, turn=turn, max_output=max_output, recovery_attempt=output_recovery
    )
    if terminal_answer is not None:
        return CanonicalResponse.create("final", "stop", {"text": terminal_answer})
    if directive:
        return CanonicalResponse.create("error", "retry", {"directive": directive})
    return CanonicalResponse.create("final", "success", {"text": result.text.strip()})


def xml_model_turn(runtime, ts, user_message: str, *, step: int, step_clock, callback=None):
    """Build text context, call the model and return the canonical parser response."""
    from agent_runtime.response import CanonicalResponse

    prompt_text = xml_build_context(runtime, ts, user_message, step=step, callback=callback)
    # _check_hard_cap 返回 <final> 字符串时直接终止（不发给模型）
    if prompt_text.startswith("<final>"):
        ts.stop_with_reason(
            StopReason.CONTEXT_OVERFLOW,
            "stopped",
            detail="hard_cap via legacy _check_hard_cap",
        )
        return CanonicalResponse.create(
            "final", "stop", {"text": runtime.hooks.complete_run(ts, prompt_text)}
        )

    runtime.hooks.notify(
        "on_pre_model",
        callback,
        step=step,
        prompt_preview=prompt_text[:200],
        path="xml",
    )
    raw, t1 = xml_call_model(runtime, ts, prompt_text, step=step, callback=callback)
    # CoT 提取：剥离思考内容后再进 history
    raw = strip_cot(raw)

    if (msg := runtime.hooks.abort_if_cancelled(ts, phase="post_model")) is not None:
        return CanonicalResponse.create("final", "stop", {"text": msg})

    t_parse = int((_time.time() - t1) * 1000)
    runtime.hooks.notify(
        "on_post_model",
        callback,
        step=step,
        raw_preview=raw[:200],
        elapsed_ms=t_parse,
        path="xml",
    )

    if (msg := runtime.hooks.maybe_step_timeout(ts, step_clock, step, "xml")) is not None:
        return CanonicalResponse.create("final", "stop", {"text": msg})

    from agent_runtime.canonical_protocol import parse_model_response

    response = parse_model_response(raw, expected_tools=set(runtime.agent.tools))
    response.payload["raw"] = raw
    return response
