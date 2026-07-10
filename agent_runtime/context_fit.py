"""System + user prompt 预算 fit（L2 repair / complete_once）。"""

from __future__ import annotations

from agent_runtime.context_projection import attach_fit_context_projection
from agent_runtime.context_metadata import merge_template_metadata
from agent_runtime.task_section import reserve_section_budget, task_preservation_metadata

__all__ = ["fit_prompt_to_budget", "fit_repair_user_prompt"]


def _fit_metadata_base(budget: TokenBudget, total_limit: int) -> dict:
    return {
        "sections": {},
        "cuts": [],
        "budget": total_limit,
        "tokenizer_backend": budget.backend,
        "tokenizer_fallback": budget.tokenizer_fallback,
        "tokenizer_id": budget.tokenizer_id,
    }


def _fit_system_to_remaining(
    budget: TokenBudget,
    system_text: str,
    remaining: int,
    metadata: dict,
) -> tuple[str, int]:
    sys_tokens = budget.count(system_text)
    if sys_tokens <= remaining:
        return system_text, sys_tokens
    if remaining <= 0:
        metadata["cuts"].append("丢弃 system（为保留 user 全文）")
        return "", 0
    fitted = budget.fit(system_text, remaining)
    sys_tokens = budget.count(fitted)
    metadata["cuts"].append(f"裁剪 system 到 {sys_tokens} tokens（保留 user 全文）")
    return fitted, sys_tokens


def fit_prompt_to_budget(
    system_text: str,
    user_text: str,
    *,
    model: str = "deepseek-v4-pro",
    provider: str = "deepseek",
    total_limit: int | None = None,
    preserve_user: bool = True,
) -> tuple[str, str, dict]:
    from agent_runtime.context_manager import TOTAL_BUDGET, TokenBudget

    if total_limit is None:
        total_limit = TOTAL_BUDGET
    budget = TokenBudget(model=model, total_limit=total_limit, provider=provider)
    metadata = _fit_metadata_base(budget, total_limit)
    system_text = system_text or ""
    user_text = user_text or ""
    user_tokens = budget.count(user_text)
    metadata["sections"]["user"] = user_tokens

    if preserve_user:
        metadata.update(task_preservation_metadata(user_tokens, total_limit))
        remaining = reserve_section_budget(total_limit, user_tokens)
        system_text, sys_tokens = _fit_system_to_remaining(
            budget, system_text, remaining, metadata
        )
        metadata["sections"]["system"] = sys_tokens
        metadata["total_tokens"] = sys_tokens + user_tokens
        attach_fit_context_projection(metadata)
        return system_text, user_text, metadata

    sys_tokens = budget.count(system_text)
    if sys_tokens >= total_limit:
        sys_cap = max(256, total_limit // 2)
        system_text = budget.fit(system_text, sys_cap)
        sys_tokens = budget.count(system_text)
        metadata["cuts"].append(f"裁剪 system 到 {sys_tokens} tokens")
    metadata["sections"]["system"] = sys_tokens

    remaining = budget.remaining(sys_tokens)
    if user_tokens > remaining:
        user_text = budget.fit(user_text, remaining)
        user_tokens = budget.count(user_text)
        metadata["cuts"].append(f"裁剪 user 到 {user_tokens} tokens（剩余预算 {remaining}）")
    metadata["sections"]["user"] = user_tokens
    metadata["total_tokens"] = sys_tokens + user_tokens
    attach_fit_context_projection(metadata)
    return system_text, user_text, metadata


def fit_repair_user_prompt(
    agent,
    user_text: str,
    system_text: str = "",
    *,
    template_meta: dict | None = None,
) -> tuple[str, dict]:
    from agent_runtime.context_manager import TOTAL_BUDGET

    config = getattr(agent, "config", None)
    model = getattr(config, "model", "deepseek-v4-pro")
    provider = getattr(config, "provider", "deepseek")
    total_limit = getattr(config, "prompt_budget", None) or TOTAL_BUDGET
    _, fitted_user, meta = fit_prompt_to_budget(
        system_text,
        user_text,
        model=model,
        provider=provider,
        total_limit=total_limit,
    )
    return fitted_user, merge_template_metadata(meta, template_meta)
