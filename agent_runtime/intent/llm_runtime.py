"""Bounded runtime for Intent Router LLM fallback calls."""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import partial
from typing import Any

from agent_runtime.cancellation import CancelledError
from agent_runtime.tool_resilience import ToolResilienceController
from agent_runtime.tool_timeout import ToolTimeoutError, run_with_timeout


@dataclass(frozen=True)
class IntentLlmPolicy:
    timeout_s: float = 8.0
    max_retries: int = 1
    retry_backoff_s: float = 0.1
    rate_limit_per_minute: int = 30
    circuit_breaker_threshold: int = 3

    def resilience_spec(self) -> dict[str, Any]:
        return {
            "max_retries": self.max_retries,
            "retry_backoff_s": self.retry_backoff_s,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "circuit_breaker_threshold": self.circuit_breaker_threshold,
            "replay_policy": "revalidate",
        }


@dataclass(frozen=True)
class IntentLlmCallResult:
    content: str = ""
    status: str = "success"
    reason: str = ""
    attempts: int = 0
    latency_ms: float = 0.0
    retryable: bool = False


def _complete(client: Any, prompt: str, max_new_tokens: int) -> str:
    try:
        return str(client.complete(prompt, max_new_tokens=max_new_tokens))
    except TypeError:
        return str(client.complete(prompt))


class IntentLlmRuntime:
    """Apply cancellation, budget, deadline, retry, rate-limit and circuit policy."""

    def __init__(
        self,
        policy: IntentLlmPolicy | None = None,
        *,
        resilience: ToolResilienceController | None = None,
    ) -> None:
        self.policy = policy or IntentLlmPolicy()
        self.resilience = resilience or ToolResilienceController()

    def complete(
        self,
        client: Any,
        prompt: str,
        *,
        cancel_token=None,
        deadline=None,
        budget=None,
        max_new_tokens: int = 384,
    ) -> IntentLlmCallResult:
        started = time.perf_counter()
        spec = self.policy.resilience_spec()
        decision = self.resilience.before("intent_llm_fallback", spec)
        if not decision.allowed:
            return self._result(
                started,
                status="degraded",
                reason=decision.reason,
                retryable=decision.reason == "rate_limited",
            )
        if cancel_token is not None and cancel_token.is_cancelled:
            return self._result(started, status="cancelled", reason="cancelled")
        if deadline is not None and getattr(deadline, "expired", lambda: False)():
            return self._result(started, status="degraded", reason="deadline_exceeded")
        if budget is not None and hasattr(budget, "reserve"):
            budget_decision = budget.reserve("llm_calls")
            if not budget_decision.allowed:
                return self._result(started, status="degraded", reason="budget_exceeded")

        attempts = self.resilience.max_attempts(spec)
        last_reason = "provider_error"
        for attempt in range(1, attempts + 1):
            timeout_s = float(self.policy.timeout_s)
            if deadline is not None and hasattr(deadline, "remaining_s"):
                remaining = deadline.remaining_s()
                if remaining is not None:
                    if remaining <= 0:
                        return self._result(
                            started,
                            status="degraded",
                            reason="deadline_exceeded",
                            attempts=attempt - 1,
                        )
                    timeout_s = min(timeout_s, max(0.05, float(remaining)))
            try:
                content = run_with_timeout(
                    partial(_complete, client, prompt, max_new_tokens),
                    timeout_s=timeout_s,
                    cancel_token=cancel_token,
                )
            except CancelledError:
                return self._result(
                    started, status="cancelled", reason="cancelled", attempts=attempt
                )
            except ToolTimeoutError:
                last_reason = "timeout"
            except Exception:
                last_reason = "provider_error"
            else:
                self.resilience.after("intent_llm_fallback", spec, success=True)
                return self._result(
                    started,
                    content=content,
                    status="success",
                    attempts=attempt,
                )

            self.resilience.after("intent_llm_fallback", spec, success=False)
            if attempt < attempts:
                delay = self.resilience.backoff_s(spec, attempt)
                if deadline is not None and hasattr(deadline, "remaining_s"):
                    remaining = deadline.remaining_s()
                    if remaining is not None:
                        delay = min(delay, max(0.0, remaining))
                if cancel_token is not None and cancel_token.is_cancelled:
                    return self._result(
                        started, status="cancelled", reason="cancelled", attempts=attempt
                    )
                if delay:
                    time.sleep(delay)
        return self._result(
            started,
            status="degraded",
            reason=last_reason,
            attempts=attempts,
            retryable=True,
        )

    @staticmethod
    def _result(started: float, **kwargs) -> IntentLlmCallResult:
        return IntentLlmCallResult(
            latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
            **kwargs,
        )


__all__ = ["IntentLlmCallResult", "IntentLlmPolicy", "IntentLlmRuntime"]
