"""Provider-neutral single model turn contract.

Providers perform one network call and return normalized content. The runtime
owns tool execution, retries, context rebuilding, and termination decisions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FinishKind(StrEnum):
    TEXT_COMPLETE = "text_complete"
    TOOL_CALLS = "tool_calls"
    MAX_OUTPUT_TOKENS = "max_output_tokens"
    CONTENT_FILTER = "content_filter"
    EMPTY_OUTPUT = "empty_output"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True)
class ProviderFinish:
    kind: FinishKind
    raw_reason: str = ""
    provider: str = ""


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""


@dataclass
class ModelTurnRequest:
    system_prompt: str
    messages: list[dict]
    tools: list[dict] = field(default_factory=list)
    max_output_tokens: int = 4096
    deadline: float | None = None

    def remaining_timeout(self, default: float) -> float:
        if self.deadline is None:
            return max(0.001, float(default))
        return max(0.001, min(float(default), self.deadline - time.monotonic()))


@dataclass
class ModelTurnResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    content: list[dict] = field(default_factory=list)
    finish: ProviderFinish = field(
        default_factory=lambda: ProviderFinish(FinishKind.EMPTY_OUTPUT)
    )
    usage: dict[str, int] = field(default_factory=dict)


def normalize_anthropic_finish(raw_reason: str, *, has_tools: bool, has_text: bool) -> FinishKind:
    """Map Anthropic-compatible stop reasons to the runtime contract."""
    reason = str(raw_reason or "").strip().lower()
    if has_tools or reason == "tool_use":
        return FinishKind.TOOL_CALLS
    if reason in {"max_tokens", "model_context_window_exceeded"}:
        return FinishKind.MAX_OUTPUT_TOKENS
    if reason in {"content_filter", "safety", "refusal"}:
        return FinishKind.CONTENT_FILTER
    if has_text and reason in {"", "end_turn", "stop_sequence", "stop"}:
        return FinishKind.TEXT_COMPLETE
    if not has_text:
        return FinishKind.EMPTY_OUTPUT
    return FinishKind.TEXT_COMPLETE


def normalize_openai_finish(raw_reason: str, *, has_text: bool) -> FinishKind:
    """Map Responses/Chat compatible statuses to the runtime contract."""
    reason = str(raw_reason or "").strip().lower()
    if reason in {"max_tokens", "length", "incomplete", "max_output_tokens"}:
        return FinishKind.MAX_OUTPUT_TOKENS
    if reason in {"content_filter", "refusal", "blocked"}:
        return FinishKind.CONTENT_FILTER
    return FinishKind.TEXT_COMPLETE if has_text else FinishKind.EMPTY_OUTPUT
