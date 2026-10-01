"""Context section 预算填充（reserve-first）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_runtime.context_manager import TokenBudget

__all__ = ["SectionFiller"]


class SectionFiller:
    """按 section_cap 填充各段；每段独立 enforcement，不只依赖 TOTAL。

    优先顺序：① section 自身 budget 硬顶 → ② section_cap 总预算剩余。
    stable 段超 cap 尝试裁剪（而非整段丢弃），写满仍溢才丢弃。
    """

    def __init__(
        self,
        budget: TokenBudget,
        metadata: dict,
        *,
        section_cap: int,
        total_limit: int,
        scaled_budget,
    ):
        self.budget = budget
        self.metadata = metadata
        self.section_cap = section_cap
        self.total_limit = total_limit
        self.scaled_budget = scaled_budget
        self.used = 0
        self.sections: dict[str, str] = {}

    def add_section(self, name: str, text: str, section_budget: int):
        """添加动态 section：先独立 section 硬顶裁剪，再总预算剩余检查。"""
        if not text:
            return
        tokens = self.budget.count(text)

        # Step 1: 独立 section 硬顶 — 超 BUDGET_* 立即裁剪（不只依赖 TOTAL）
        if section_budget > 0 and tokens > section_budget:
            text = self.budget.fit(text, section_budget)
            tokens = self.budget.count(text)
            self.metadata["cuts"].append(
                f"裁剪 {name} 到 {tokens} tokens（section 预算 {section_budget}）"
            )

        # Step 2: 总预算剩余检查
        remaining = self.section_cap - self.used
        if remaining <= 0:
            self.metadata["cuts"].append(f"跳过 {name}（task 预留后预算耗尽）")
            self.metadata["sections"][name] = 0
            return
        if tokens > remaining:
            text = self.budget.fit(text, remaining)
            tokens = self.budget.count(text)
            self.metadata["cuts"].append(
                f"裁剪 {name} 到 {tokens} tokens（总预算剩余 {remaining}）"
            )

        self.used += tokens
        self.metadata["sections"][name] = tokens
        self.sections[name] = text

    def add_required(self, sections: dict[str, str]) -> None:
        """Reserve complete required sections together; never prefix-truncate them."""
        from agent_runtime.errors import ContextBuildBlockedError

        counts = {name: self.budget.count(text) for name, text in sections.items() if text}
        if set(counts) & set(self.sections):
            raise ContextBuildBlockedError("context_required_duplicate_section")
        tokens = sum(counts.values())
        if tokens + self.used > self.section_cap:
            raise ContextBuildBlockedError(
                "context_required_over_budget",
                metadata={
                    "required_tokens": tokens,
                    "available_tokens": self.section_cap - self.used,
                    "required_sections": counts,
                },
            )
        for name, tokens in counts.items():
            self.sections[name] = sections[name]
            self.metadata["sections"][name] = tokens
            self.used += tokens
        self.metadata["required_sections"] = counts
        self.metadata["required_tokens"] = sum(counts.values())

    def add_elastic(self, candidates, weights, priority, *, fitters=None):
        """Allocate soft quotas, then lend unused capacity in priority order.

        Fitters can preserve complete source snippets or tool message groups.
        They must be pure: allocation never fetches or summarizes content.
        """
        if set(candidates) & set(self.sections):
            raise ValueError("duplicate_context_section")
        fitters = fitters or {}
        pool = max(0, self.section_cap - self.used)
        weight_sum = sum(weights.values()) or 1
        quotas = {name: int(pool * weights.get(name, 0) / weight_sum) for name in priority}
        allocations = {}

        def fit(name, limit):
            if limit <= 0:
                return "", 0
            text = fitters.get(name, self.budget.fit)(candidates.get(name, ""), limit)
            cost = self.budget.count(text)
            if cost > limit:
                raise ValueError("context_fitter_exceeds_budget")
            return text, cost

        for name in priority:
            allocations[name] = fit(name, quotas[name])
        remaining = pool - sum(cost for _, cost in allocations.values())
        for name in priority:
            previous = allocations[name][1]
            allocations[name] = fit(name, previous + remaining)
            remaining -= allocations[name][1] - previous
        decisions = {}
        for name, (text, cost) in allocations.items():
            if text:
                self.sections[name] = text
            self.metadata["sections"][name] = cost
            self.used += cost
            demand = self.budget.count(candidates.get(name, ""))
            decisions[name] = {
                "soft_quota": quotas[name],
                "demand_tokens": demand,
                "used_tokens": cost,
                "borrowed_tokens": max(0, cost - quotas[name]),
                "released_tokens": max(0, quotas[name] - cost),
                "reason": "empty" if not demand else "complete" if cost == demand else "budget",
            }
            if cost < demand:
                self.metadata["cuts"].append(f"elastic:{name}:budget")
        self.metadata["elastic_budget"] = {
            "pool_tokens": pool,
            "unused_tokens": remaining,
            "priority": list(priority),
            "allocations": decisions,
        }

    def add_stable_section(self, name: str, text: str, section_limit: int):
        """添加 stable section：超 cap 尝试裁剪，写满仍溢才丢弃。"""
        if not text:
            self.metadata["sections"][name] = 0
            return
        tokens = self.budget.count(text)
        cap = self.scaled_budget(section_limit, self.section_cap or self.total_limit)

        # Step 1: 独立 section 硬顶 — 超 cap 尝试裁剪（而非整段丢弃）
        if tokens > cap:
            text = self.budget.fit(text, cap)
            tokens = self.budget.count(text)
            self.metadata["cuts"].append(
                f"裁剪 {name} 到 {tokens} tokens（stable section cap {cap}）"
            )

        # Step 2: 总预算剩余检查
        if self.used + tokens > self.section_cap:
            remaining = max(0, self.section_cap - self.used)
            if remaining > 0:
                text = self.budget.fit(text, remaining)
                tokens = self.budget.count(text)
                self.metadata["cuts"].append(f"裁剪 {name} 到 {tokens} tokens（剩余 {remaining}）")
            else:
                self.metadata["cuts"].append(f"丢弃 {name}（task 预留后预算不足）")
                self.metadata["sections"][name] = 0
                return

        self.used += tokens
        self.metadata["sections"][name] = tokens
        self.sections[name] = text
