"""Provider-neutral dispatch gate. Reservations survive unknown or interrupted calls."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable

from agent_runtime.budget_manager import BudgetManager
from agent_runtime.cancellation import run_blocking


class ModelBudgetExceededError(ValueError):
    pass


# Usage counters are client-owned. Serialize dispatch and counter reads for shared clients.
_lock_guard = threading.Lock()


def _client_lock(client):
    with _lock_guard:
        lock = getattr(client, "_model_budget_lock", None)
        if lock is None:
            lock = threading.RLock()
            client._model_budget_lock = lock
        return lock


def _costs(inp, out):
    return {"llm_calls": 1, "prompt_tokens": inp, "output_tokens": out, "total_tokens": inp + out}


def invoke_model_call(
    fn: Callable,
    *,
    budget: BudgetManager,
    client,
    input_tokens: int,
    output_tokens: int,
    purpose: str,
    cancel_token=None,
    deadline=None,
    timeout_s=None,
    input_limit=None,
    on_reserved=None,
):
    """One runtime dispatch, including provider-internal retries, is one call.

    Settle only on the owner thread. A late worker after cancellation cannot
    refund an unknown reservation or mutate the restored budget.
    """
    if purpose not in {"repair", "planning", "exploration", "extraction", "summary"}:
        raise ValueError("invalid_model_call_purpose")
    if input_tokens < 0 or output_tokens <= 0:
        raise ValueError("invalid_model_call_reservation")
    if input_limit is not None and input_tokens > input_limit:
        raise ValueError("model_input_window_exceeded")
    if timeout_s is not None and timeout_s <= 0:
        raise TimeoutError("model_call_deadline_exceeded")
    if cancel_token is not None:
        cancel_token.check()
    if deadline is not None:
        deadline.check()
        remaining = deadline.remaining_s()
        if remaining is not None:
            timeout_s = remaining if timeout_s is None else min(timeout_s, remaining)
    key = "model:" + uuid.uuid4().hex
    reserved = _costs(input_tokens, output_tokens)
    if not budget.reserve_model_call(key, purpose, reserved):
        raise ModelBudgetExceededError("model_call_budget_exhausted")
    if on_reserved is not None:
        on_reserved(budget.snapshot())

    def dispatch():
        with _client_lock(client):
            if cancel_token is not None:
                cancel_token.check()
            if deadline is not None:
                deadline.check()
            before = dict(getattr(client, "session_usage", {}) or {})
            result = fn()
            usage = getattr(result, "usage", {}) or {}
            after = dict(getattr(client, "session_usage", {}) or {})
            if (
                not hasattr(result, "usage")
                and after.get("calls", 0) > before.get("calls", 0)
                and all(type(after.get(k)) is int for k in ("input_tokens", "output_tokens"))
            ):
                usage = {
                    k: after.get(k, 0) - before.get(k, 0) for k in ("input_tokens", "output_tokens")
                }
            known = all(
                type(usage.get(k)) is int and usage[k] >= 0
                for k in ("input_tokens", "output_tokens")
            )
            return result, usage if known else None

    result, usage = run_blocking(dispatch, cancel_token=cancel_token, timeout_s=timeout_s)
    if usage is not None:
        budget.settle_model_call(
            key, _costs(usage["input_tokens"], usage["output_tokens"]), "actual"
        )
    else:
        budget.settle_model_call(key, reserved, "estimated")
    return result


def complete_text(
    agent,
    client,
    prompt: str,
    *,
    max_output_tokens: int,
    purpose: str,
    timeout_s=None,
    on_reserved=None,
):
    """Auxiliary calls share the current run ledger, cancellation and deadline."""
    from agent_runtime.context_manager import TokenBudget

    budget = getattr(agent, "_run_budget_manager", None)
    if budget is None:
        budget = BudgetManager.from_config(agent.config)
        agent._run_budget_manager = budget
        agent._standalone_model_budget = True
    counter = TokenBudget(model=agent.config.model, provider=agent.config.provider)
    deadline = getattr(agent, "_repair_deadline", None)
    if deadline is None:
        from agent_runtime.repair_runtime import ExecutionDeadline

        deadline = ExecutionDeadline(agent.config.deadline.repair_s)
        agent._repair_deadline = deadline
    try:
        return invoke_model_call(
            lambda: client.complete(prompt, max_new_tokens=max_output_tokens),
            budget=budget,
            client=client,
            input_tokens=counter.count(prompt),
            output_tokens=max_output_tokens,
            purpose=purpose,
            cancel_token=getattr(agent, "cancel_token", None),
            deadline=deadline,
            timeout_s=timeout_s,
            input_limit=agent.config.input_window(max_output_tokens),
            on_reserved=on_reserved,
        )
    finally:
        agent.session["runtime_budget"] = budget.snapshot()
