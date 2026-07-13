"""Repair 流水线共享预算上下文（V1.4-Bonus3c）。

提供跨 Agent 的 TokenBudget 共享与分角色子预算分配，
避免每个 Agent 独立创建 TokenBudget 的重复开销。

Usage::

    budget_ctx = RepairBudgetContext.create(
        model="deepseek-v4-pro", provider="deepseek",
    )
    # 各 Agent 获取子预算视图
    loc_budget = budget_ctx.sub_budget("localizer")   # 2000 tokens
    ret_budget = budget_ctx.sub_budget("retriever")   # 3000 tokens
    pat_budget = budget_ctx.sub_budget("patcher")     # 4000 tokens
    # 注入 ContextManager
    cm = ContextManager(agent, budget=loc_budget)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_runtime.context_manager import TOTAL_BUDGET, TokenBudget

# 分 Agent 预算表（prompt_budget，token 数）
_DEFAULT_ALLOCATIONS: dict[str, int] = {
    "localizer": 2000,
    "retriever": 3000,
    "patcher": 4000,
    "verifier": 1000,
    "baseline": 6000,
}

_MASTER_BUDGET = 100_000


@dataclass
class RepairBudgetContext:
    """Repair 流水线共享预算上下文。

    持有一个 master TokenBudget + 分 Agent 子预算表。
    Agent 的 ContextManager 通过 ``sub_budget(role)`` 获取角色专属预算。
    """

    master: TokenBudget
    allocations: dict[str, int] = field(default_factory=lambda: dict(_DEFAULT_ALLOCATIONS))
    _usage: dict[str, dict[str, int]] = field(default_factory=dict)

    # ---- 工厂 ----

    @classmethod
    def create(
        cls,
        model: str = "deepseek-v4-pro",
        provider: str = "deepseek",
        *,
        allocations: dict[str, int] | None = None,
    ) -> RepairBudgetContext:
        """创建共享预算上下文。

        Args:
            model: 模型名（用于选择 tokenizer）。
            provider: 提供商名。
            allocations: 分角色预算表。None 使用默认值。
        """
        master = TokenBudget(
            model=model,
            total_limit=_MASTER_BUDGET,
            provider=provider,
        )
        merged = dict(_DEFAULT_ALLOCATIONS)
        if allocations:
            merged.update(allocations)
        return cls(
            master=master,
            allocations=merged,
        )

    # ---- 子预算 ----

    def sub_budget(self, role: str) -> TokenBudget:
        """返回指定角色的子预算视图。

        子预算复用 master 的 tokenizer（``_counter``），仅 ``total_limit`` 不同。
        不同角色之间预算独立，不互相影响。

        Args:
            role: Agent 角色名（localizer/retriever/patcher/verifier/baseline）。

        Returns:
            角色专属的 TokenBudget 实例。
        """
        limit = self.allocations.get(role, TOTAL_BUDGET)
        budget = TokenBudget(
            model=self.master.model,
            total_limit=limit,
            provider=self.master.provider,
        )
        return budget

    # ---- 消耗追踪 ----

    def track_usage(self, role: str, input_tokens: int, output_tokens: int) -> None:
        """记录指定角色的一次 API 调用消耗。

        Args:
            role: Agent 角色名。
            input_tokens: 输入 token 数。
            output_tokens: 输出 token 数。
        """
        entry = self._usage.setdefault(role, {"input_tokens": 0, "output_tokens": 0, "calls": 0})
        entry["input_tokens"] += input_tokens
        entry["output_tokens"] += output_tokens
        entry["calls"] += 1

    @property
    def usage_summary(self) -> dict[str, dict[str, int]]:
        """返回分 Agent 的累计消耗快照（只读副本）。"""
        return {k: dict(v) for k, v in self._usage.items()}

    @property
    def total_input(self) -> int:
        """所有 Agent 的累计输入 token 数。"""
        return sum(v.get("input_tokens", 0) for v in self._usage.values())

    @property
    def total_output(self) -> int:
        """所有 Agent 的累计输出 token 数。"""
        return sum(v.get("output_tokens", 0) for v in self._usage.values())
