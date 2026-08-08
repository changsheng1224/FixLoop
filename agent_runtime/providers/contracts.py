"""Provider-neutral model capability and error contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class ProviderErrorCode(StrEnum):
    AUTHENTICATION = "authentication_error"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    NETWORK = "network_error"
    PROTOCOL = "protocol_error"
    EMPTY_RESPONSE = "empty_response"
    UNKNOWN = "provider_error"


@dataclass(frozen=True)
class ProviderCapabilities:
    """Features exposed by a concrete model client."""

    provider: str
    model: str = ""
    native_tools: bool = False
    streaming: bool = False
    usage: bool = False
    prompt_cache: bool = False
    cancellation: bool = False


class ProviderError(RuntimeError):
    """Normalized provider failure consumed by retry and termination logic."""

    def __init__(
        self,
        code: ProviderErrorCode | str,
        message: str,
        *,
        retryable: bool = False,
        retry_after_s: float | None = None,
        provider: str = "",
        cause: Exception | None = None,
    ):
        super().__init__(message)
        self.code = ProviderErrorCode(str(code))
        self.retryable = bool(retryable)
        self.retry_after_s = retry_after_s
        self.provider = provider
        self.cause = cause


class ModelClient(Protocol):
    """Minimum provider contract required by Agent Runtime."""

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    def complete(self, prompt: str, max_new_tokens: int = 512, **kwargs: Any) -> str: ...


def normalize_provider_error(exc: Exception, *, provider: str = "") -> ProviderError:
    """Map common provider exceptions to a deterministic runtime error."""
    from agent_runtime.errors import EmptyModelResponse
    from agent_runtime.providers.retry_policy import RateLimitExceededError

    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, EmptyModelResponse):
        return ProviderError(
            ProviderErrorCode.EMPTY_RESPONSE,
            str(exc),
            retryable=True,
            provider=provider,
            cause=exc,
        )
    if isinstance(exc, RateLimitExceededError):
        return ProviderError(
            ProviderErrorCode.RATE_LIMIT,
            str(exc),
            retryable=True,
            provider=provider,
            cause=exc,
        )
    name = type(exc).__name__.lower()
    if "timeout" in name:
        code = ProviderErrorCode.TIMEOUT
        retryable = True
    elif "url" in name or "connection" in name or "network" in name:
        code = ProviderErrorCode.NETWORK
        retryable = True
    elif "json" in name or "protocol" in name:
        code = ProviderErrorCode.PROTOCOL
        retryable = False
    else:
        code = ProviderErrorCode.UNKNOWN
        retryable = False
    return ProviderError(code, str(exc), retryable=retryable, provider=provider, cause=exc)


__all__ = [
    "ModelClient",
    "ProviderCapabilities",
    "ProviderError",
    "ProviderErrorCode",
    "normalize_provider_error",
]
