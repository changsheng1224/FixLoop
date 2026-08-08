"""Bounded sequential Skill composition with cancellation propagation."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.cancellation import CancellationToken, CancelledError
from src.skills.execution import SkillExecutionGateway
from src.skills.invocation import SkillExecutionResult

InputMapper = Callable[[dict[str, Any], dict[str, SkillExecutionResult]], dict[str, Any]]


@dataclass(frozen=True)
class SkillStep:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    input_mapper: InputMapper | None = None
    continue_on_failure: bool = False
    pinned_version: str = ""
    runner: Callable[..., dict[str, Any]] | None = None
    tool_bindings: dict[str, Callable[..., Any]] = field(default_factory=dict)
    runtime_allowed_tools: set[str] = field(default_factory=set)
    read_before_write: bool = False


@dataclass
class SkillCompositionResult:
    status: str
    steps: list[SkillExecutionResult] = field(default_factory=list)
    outputs: dict[str, SkillExecutionResult] = field(default_factory=dict)
    reason: str = ""


class SkillComposer:
    def __init__(
        self,
        gateway: SkillExecutionGateway,
        *,
        max_depth: int = 8,
        max_total_tool_calls: int = 32,
        max_total_s: float = 300.0,
    ) -> None:
        self.gateway = gateway
        self.max_depth = max(1, max_depth)
        self.max_total_tool_calls = max(0, max_total_tool_calls)
        self.max_total_s = max(0.01, max_total_s)

    def run(
        self,
        steps: list[SkillStep],
        *,
        initial: dict[str, Any] | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> SkillCompositionResult:
        if len(steps) > self.max_depth:
            return SkillCompositionResult("failed", reason="max_composition_depth")
        seen: set[tuple[str, str]] = set()
        context = dict(initial or {})
        result = SkillCompositionResult("running")
        started = time.monotonic()
        total_tool_calls = 0
        for index, step in enumerate(steps):
            try:
                if cancel_token is not None:
                    cancel_token.check()
            except CancelledError as exc:
                result.status = "cancelled"
                result.reason = str(exc)
                return result
            if time.monotonic() - started >= self.max_total_s:
                result.status = "timed_out"
                result.reason = "composition_timeout"
                return result
            identity = (step.name, step.pinned_version)
            if identity in seen:
                result.status = "failed"
                result.reason = "composition_cycle"
                return result
            seen.add(identity)
            args = dict(step.args)
            if step.input_mapper is not None:
                args.update(step.input_mapper(context, result.outputs))
            item = self.gateway.execute(
                step.name,
                args,
                pinned_version=step.pinned_version,
                runner=step.runner,
                tool_bindings=step.tool_bindings,
                runtime_allowed_tools=step.runtime_allowed_tools,
                read_before_write=step.read_before_write,
                cancel_token=cancel_token,
                idempotency_key=(
                    f"composition:{index}:{step.name}:"
                    + hashlib.sha256(
                        json.dumps(args, sort_keys=True, default=str).encode()
                    ).hexdigest()[:12]
                ),
            )
            result.steps.append(item)
            result.outputs[step.name] = item
            context[step.name] = item.output
            total_tool_calls += len(item.invocation.tool_calls)
            if total_tool_calls > self.max_total_tool_calls:
                result.status = "failed"
                result.reason = "composition_tool_budget_exhausted"
                return result
            if not item.ok and not step.continue_on_failure:
                result.status = "failed"
                result.reason = item.invocation.error_code or item.invocation.status
                return result
        result.status = "succeeded"
        return result
