"""Context section 预算填充（reserve-first）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_runtime.context_manager import TokenBudget

__all__ = ["SectionFiller"]


class SectionFiller:
    """按 section_cap 填充各段；stable 段超 cap 整段丢弃并记 0 token。"""

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
        if not text:
            return
        tokens = self.budget.count(text)
        remaining = self.section_cap - self.used
        if remaining <= 0:
            self.metadata["cuts"].append(f"跳过 {name}（task 预留后预算耗尽）")
            self.metadata["sections"][name] = 0
            return
        limit = min(remaining, section_budget) if section_budget > 0 else remaining
        if tokens > limit:
            text = self.budget.fit(text, limit)
            tokens = self.budget.count(text)
            self.metadata["cuts"].append(f"裁剪 {name} 到 {tokens} tokens")
        self.used += tokens
        self.metadata["sections"][name] = tokens
        self.sections[name] = text

    def add_stable_section(self, name: str, text: str, section_limit: int):
        if not text:
            self.metadata["sections"][name] = 0
            return
        tokens = self.budget.count(text)
        cap = self.scaled_budget(section_limit, self.section_cap or self.total_limit)
        if tokens > cap:
            self.metadata["cuts"].append(f"丢弃 {name}（{tokens} > section cap {cap}）")
            self.metadata["sections"][name] = 0
            return
        if self.used + tokens > self.section_cap:
            self.metadata["cuts"].append(f"丢弃 {name}（task 预留后预算不足）")
            self.metadata["sections"][name] = 0
            return
        self.used += tokens
        self.metadata["sections"][name] = tokens
        self.sections[name] = text
