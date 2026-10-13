"""Explicit task-only context policy for isolated agents of any purpose."""

import json

from agent_runtime.context_preparation import request_hash
from agent_runtime.model_turn import ModelTurnRequest
from agent_runtime.token_budget import TokenBudget


class IsolatedContextManager:
    def __init__(self, agent):
        self.agent = agent
        self.budget = TokenBudget(
            model=agent.config.model,
            provider=agent.config.provider,
            total_limit=agent.config.prompt_budget,
        )

    def prepare_request(
        self,
        user_message,
        *,
        protocol,
        tools=(),
        native_tail=(),
        max_output_tokens=2048,
        deadline=None,
        **options,
    ):
        # No workspace documents, memory, source retrieval or another session's history.
        system = self.agent.prefix.text
        original = self.agent.session["history"][0]["content"]
        if protocol == "native":
            messages = [{"role": "user", "content": original}, *native_tail]
        else:
            history = self.agent.session["history"]
            messages = [
                {
                    "role": "user",
                    "content": system
                    + "\n"
                    + json.dumps(list(tools))
                    + "\n"
                    + json.dumps(history, ensure_ascii=False),
                }
            ]
            system = ""
        request = ModelTurnRequest(
            system, messages, list(tools), max_output_tokens=max_output_tokens, deadline=deadline
        )
        tokens = self.budget.count(system + json.dumps(messages) + json.dumps(list(tools))) + 64
        metadata = {
            "total_tokens": tokens,
            "provider_input_tokens": tokens,
            "request_hash": request_hash(request, protocol),
            "protocol": protocol,
            "sections": {},
            "prompt_budget": self.agent.config.prompt_budget,
        }
        return request, metadata

    def validate_prepared_request(self, request, metadata):
        if request_hash(request, metadata["protocol"]) != metadata["request_hash"]:
            raise ValueError("isolated_context_request_changed")
