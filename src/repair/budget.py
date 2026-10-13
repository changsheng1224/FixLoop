"""Configured context-budget views; cumulative accounting lives in BudgetManager."""

from dataclasses import dataclass

from agent_runtime.token_budget import TokenBudget


@dataclass
class RepairBudgetContext:
    master: TokenBudget
    allocations: dict[str, int]

    @classmethod
    def create(cls, model, provider, *, allocations):
        if not allocations or any(value <= 0 for value in allocations.values()):
            raise ValueError("explicit context allocations required")
        return cls(
            TokenBudget(model=model, provider=provider, total_limit=max(allocations.values())),
            dict(allocations),
        )

    def sub_budget(self, role):
        return TokenBudget(
            model=self.master.model,
            provider=self.master.provider,
            total_limit=self.allocations[role],
        )
