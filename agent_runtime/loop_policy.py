"""Application-injected decisions; the L1 loop owns execution and persistence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LoopPolicyContext:
    agent: Any
    state: Any
    guard: Any
    task_state: Any
    emit: Callable
    grant_read_reserve: Callable
    matching_read_reservation: Callable
    has_targeted_read_reserve: Callable


class LoopPolicy:
    """Default policy for a general-purpose Agent; no role-name dispatch."""

    localization_mode = False
    action_directive = "[ACTION REQUIRED] Complete the next concrete tool action."
    stall_hint = ""

    def reset(self, context: LoopPolicyContext) -> None:
        pass

    def visible_tools(self, context, *, action_required=False, strict_recovery=False):
        return None

    def preflight(self, context, tool_name, tool_args, *, step):
        return None

    def enter_convergence(self, context, reason: str, *, step: int) -> None:
        pass

    def review_result(self, context, tool_name, tool_args, result):
        pass

    def on_result(self, context, tool_name, tool_args, result, *, step):
        pass

    def on_success(self, context, tool_name, tool_args, result, *, step):
        context.state.action_required = False
        context.state.recovery_directive = ""
        context.state.recovery_allowed_tools = None
        context.state.recovery_kind = ""

    def feedback(self, context, tool_name, result) -> str:
        return result.content

    def has_progress(self, context, tool_name, result) -> bool:
        side_effect = context.agent.tools.get(tool_name, {}).get("side_effect", "read")
        return bool(result.changed_files) or (result.ok and side_effect in {"read", "none"})

    def output_recovery(self, *, truncated: bool) -> str:
        prefix = "[OUTPUT RECOVERY]" if truncated else "[EMPTY OUTPUT RECOVERY]"
        return f"{prefix} The previous output was discarded. Produce one complete tool call."

    def recovery_anchors(self, text: str) -> str:
        return ""
