"""Fresh, tool-free L1 chat turns; no project or previous Agent session access."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from agent_runtime.bootstrap import create_model_client
from agent_runtime.cancellation import CancellationToken
from agent_runtime.model_turn import FinishKind, ModelTurnRequest, ModelTurnResult
from agent_runtime.providers.chat import stream_chat_turn
from agent_runtime.providers.contracts import ProviderError, ProviderErrorCode

DEFAULT_SYSTEM_PROMPT = (
    "You are FixLoop, a helpful conversational assistant. Answer questions, discuss ideas, "
    "and help with writing in the user's language. You have no access to project files or tools. "
    "Return your answer directly as plain text or Markdown."
)


class ChatExecutor:
    """Bound live model I/O, including cancelled transports that have not exited.

    Cancellation returns promptly even during a blocking HTTP read. A detached
    daemon transport has no tools, and its token fences every later output. It
    retains its I/O slot until it exits; repeated stops cannot spawn unbounded I/O.
    """

    def __init__(
        self,
        client_factory: Callable | None = None,
        *,
        provider: str = "anthropic_compat",
        model: str | None = None,
        base_url: str | None = None,
        max_output_tokens: int = 4096,
        timeout_s: float = 120,
        max_context_bytes: int = 400_000,
        max_transports: int = 4,
    ):
        if provider not in {"anthropic_compat", "openai", "ollama", "fake"}:
            raise ValueError("unsupported chat provider")
        if not 1 <= max_output_tokens <= 8192 or timeout_s <= 0 or max_context_bytes < 1:
            raise ValueError("invalid chat limits")
        if max_transports < 1:
            raise ValueError("max_transports must be positive")
        self._factory = client_factory or (
            lambda: create_model_client(provider=provider, model=model, base_url=base_url)
        )
        self._slots = threading.BoundedSemaphore(max_transports)
        self.max_output_tokens = max_output_tokens
        self.timeout_s = timeout_s
        self.max_context_bytes = max_context_bytes

    def __call__(self, messages, token: CancellationToken, on_chunk) -> ModelTurnResult:
        messages = [dict(message) for message in messages]
        size = sum(len(m["content"].encode("utf-8")) for m in messages)
        if size > self.max_context_bytes:
            raise ValueError("chat context limit exceeded; start or fork a shorter conversation")
        deadline = time.monotonic() + self.timeout_s
        token.check()
        while not self._slots.acquire(timeout=0.05):
            token.check()
            if time.monotonic() >= deadline:
                raise TimeoutError("chat deadline exceeded waiting for model capacity")
        done = threading.Event()
        outcome: list = []

        def dispatch():
            try:
                token.check()
                request = ModelTurnRequest(
                    system_prompt=DEFAULT_SYSTEM_PROMPT,
                    messages=messages,
                    max_output_tokens=self.max_output_tokens,
                    deadline=deadline,
                )
                outcome.append(
                    stream_chat_turn(
                        self._factory(), request, cancel_token=token, on_chunk=on_chunk
                    )
                )
            except BaseException as exc:
                outcome.append(exc)
            finally:
                self._slots.release()
                done.set()

        try:
            threading.Thread(target=dispatch, name="workbench-model-io", daemon=True).start()
        except BaseException:
            self._slots.release()
            raise
        while not done.wait(0.05):
            token.check()
            if time.monotonic() >= deadline:
                token.cancel("timeout")
                raise TimeoutError("chat deadline exceeded")
        token.check()
        result = outcome[0]
        if isinstance(result, BaseException):
            raise result
        if result.finish.kind != FinishKind.TEXT_COMPLETE or not result.text.strip():
            raise ProviderError(
                ProviderErrorCode.PROTOCOL,
                "chat did not complete: " + result.finish.kind.value,
            )
        return result
