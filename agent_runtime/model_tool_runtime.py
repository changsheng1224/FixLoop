"""Role-neutral model/protocol/tool execution, also used by isolated tasks."""

from __future__ import annotations

import json
from dataclasses import dataclass

from agent_runtime.canonical_protocol import parse_model_response
from agent_runtime.model_call_budget import invoke_model_call


@dataclass(frozen=True)
class LoopOutcome:
    text: str = ""
    stop_reason: str = "model_turn_limit"
    tool_calls: int = 0
    model_turns: int = 0


class ModelToolRuntime:
    @staticmethod
    def invoke(fn, **kwargs):
        return invoke_model_call(fn, **kwargs)

    @staticmethod
    def dispatch(call, execute, *, cancel_token=None):
        return call.argument_rejection() or ModelToolRuntime.execute(
            execute,
            call.tool_name,
            call.arguments,
            call_context=call.context,
            cancel_token=cancel_token,
        )

    @staticmethod
    def execute(execute, tool_name, arguments, *, call_context=None, cancel_token=None):
        if cancel_token is not None:
            cancel_token.check()
        if call_context is None:
            return execute(tool_name, arguments)
        return execute(tool_name, arguments, call_context=call_context)

    def run(
        self,
        *,
        agent,
        messages,
        budget,
        deadline,
        run_id,
        on_tool,
        event,
    ):
        """Run isolated tasks through the same AgentLoop as ordinary tasks."""
        from copy import copy
        from types import SimpleNamespace

        agent.bind_budget(budget, deadline)
        agent.shared_run_id = run_id
        agent.workspace_pinned = True

        def transform(name, arguments, result):
            visible = on_tool(SimpleNamespace(tool_name=name, arguments=arguments), result)
            rendered = copy(result)
            rendered.content = json.dumps(visible, ensure_ascii=False)
            return rendered

        def events(name, payload):
            mapped = {
                "model_request_start": "subagent_model_started",
                "provider_finish": "subagent_model_completed",
                "tool_call": "subagent_tool_started",
                "tool_executed": "subagent_tool_completed",
            }
            if name in mapped:
                event(mapped[name], **payload)

        agent.tool_result_transform = transform
        agent.event_sink = events
        try:
            answer = agent.ask(messages[0]["content"], skip_plan=True)
        finally:
            agent.tool_result_transform = None
            agent.event_sink = None
            agent.bind_budget(None, None)
        state = agent.last_task_state
        reason = str(state.stop_reason) if state is not None else "runtime_failed"
        if reason == "final":
            response = parse_model_response(answer)
            answer = response.payload.get("text", answer)
        return LoopOutcome(
            answer,
            "completed" if reason == "final" else reason,
            int(budget.summary().get("tool_calls", {}).get("used", 0)),
            int(budget.summary().get("llm_calls", {}).get("used", 0)),
        )
