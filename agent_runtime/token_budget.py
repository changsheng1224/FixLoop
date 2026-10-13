"""Token accounting primitives shared by context assembly and its consumers.

These lived in `context_manager`, the largest module in the layer. That made
every consumer either import that module or reach back into it from inside a
function to dodge the resulting cycle -- `compression_pipeline` needed the
workaround on its per-tool truncation hot path. None of this belongs to context
assembly in particular, so it lives on its own and both sides import it.
"""

from __future__ import annotations

from agent_runtime.tokenizers import resolve_token_counter, resolve_tokenizer_spec

# 参考布局下的总预算；actual limits scale the per-section budgets below.
REF_TOTAL_BUDGET = 6000
TOTAL_BUDGET = 100000
BUDGET_HISTORY = 2600


def scaled_section_budget(section_limit: int, total_limit: int) -> int:
    """将参考布局下的 section 预算缩放到实际 total_limit。"""
    return max(1, int(section_limit * total_limit / REF_TOTAL_BUDGET))


def history_window_budget(total_limit: int) -> int:
    """history section 预算 = 压缩管线 window（L2–L5 百分比阈值基准）。"""
    return scaled_section_budget(BUDGET_HISTORY, total_limit)


class TokenBudget:
    """Token 精确计数器（多 backend：DeepSeek HF / OpenAI tiktoken）。"""

    def __init__(
        self,
        model: str = "deepseek-v4-pro",
        total_limit: int = TOTAL_BUDGET,
        provider: str = "deepseek",
    ):
        self.total_limit = total_limit
        self.model = model
        self.provider = provider
        self._counter = resolve_token_counter(model, provider)
        self.backend = self._counter.backend
        self._spec = resolve_tokenizer_spec(model, provider)
        self.tokenizer_fallback = self._spec.fallback
        self.tokenizer_id = self._spec.tokenizer_id

    def count(self, text: str) -> int:
        """返回文本的 token 数。"""
        return self._counter.count(text)

    def fit(self, text: str, limit: int) -> str:
        """将文本截断到指定 token 限制以内。"""
        return self._counter.fit(text, limit)

    def remaining(self, used: int) -> int:
        """返回剩余 token 预算。"""
        return max(0, self.total_limit - used)
