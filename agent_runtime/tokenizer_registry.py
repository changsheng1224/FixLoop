"""显式 model/provider → tokenizer 规则表。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

BackendKind = Literal["tiktoken", "huggingface"]
RuleKind = Literal["exact", "prefix", "provider"]


@dataclass(frozen=True)
class TokenRule:
    """单条 registry 规则。"""

    id: str
    kind: RuleKind
    pattern: str
    backend: BackendKind
    encoding: str | None = None
    tokenizer_id: str | None = None
    dynamic_deepseek: bool = False
    warn: str | None = None
    provider: str | None = None


# 匹配顺序：exact → prefix → provider → GLOBAL_FALLBACK
REGISTRY: tuple[TokenRule, ...] = (
    TokenRule("openai-gpt4o", "exact", "gpt-4o", "tiktoken", encoding="cl100k_base"),
    TokenRule("openai-gpt4", "exact", "gpt-4", "tiktoken"),
    TokenRule("openai-gpt-prefix", "prefix", "gpt-", "tiktoken"),
    TokenRule("openai-provider", "provider", "openai", "tiktoken"),
    TokenRule("deepseek-model", "prefix", "deepseek", "huggingface", dynamic_deepseek=True),
    TokenRule("deepseek-provider", "provider", "deepseek", "huggingface", dynamic_deepseek=True),
    TokenRule(
        "anthropic-compat",
        "provider",
        "anthropic_compat",
        "huggingface",
        dynamic_deepseek=True,
    ),
    TokenRule(
        "claude-prefix",
        "prefix",
        "claude-",
        "tiktoken",
        encoding="cl100k_base",
        warn="approximate_tokenizer",
    ),
    TokenRule(
        "anthropic-provider",
        "provider",
        "anthropic",
        "tiktoken",
        encoding="cl100k_base",
        warn="approximate_tokenizer",
    ),
    TokenRule(
        "ollama-qwen",
        "prefix",
        "qwen",
        "huggingface",
        tokenizer_id="Qwen/Qwen2.5-0.5B-Instruct",
        provider="ollama",
    ),
    TokenRule("ollama-provider", "provider", "ollama", "tiktoken"),
)

GLOBAL_FALLBACK = TokenRule(
    "global-fallback",
    "exact",
    "*",
    "tiktoken",
    encoding="cl100k_base",
    warn="unknown_model_fallback",
)


@dataclass(frozen=True)
class TokenizerSpec:
    """resolve 结果（不含 counter 实例）。"""

    rule_id: str
    backend_kind: BackendKind
    encoding: str | None
    tokenizer_id: str | None
    fallback: bool
    warn: str | None


def _rule_matches(rule: TokenRule, model_lower: str, provider_lower: str) -> bool:
    if rule.provider is not None and provider_lower != rule.provider.lower():
        return False
    if rule.kind == "exact":
        return model_lower == rule.pattern.lower()
    if rule.kind == "prefix":
        return model_lower.startswith(rule.pattern.lower())
    if rule.kind == "provider":
        return provider_lower == rule.pattern.lower()
    return False


def lookup_token_rule(model: str, provider: str) -> tuple[TokenRule, bool]:
    """查 registry，返回 (rule, is_global_fallback)。"""
    model_lower = (model or "").lower()
    provider_lower = (provider or "").lower()

    for rule in REGISTRY:
        if rule.kind == "exact" and _rule_matches(rule, model_lower, provider_lower):
            return rule, False

    for rule in REGISTRY:
        if rule.kind == "prefix" and _rule_matches(rule, model_lower, provider_lower):
            return rule, False

    for rule in REGISTRY:
        if rule.kind == "provider" and _rule_matches(rule, model_lower, provider_lower):
            return rule, False

    return GLOBAL_FALLBACK, True


def build_tokenizer_spec(
    model: str,
    provider: str,
    *,
    resolve_deepseek_id,
) -> TokenizerSpec:
    """由 model/provider 构建 TokenizerSpec。"""
    rule, is_fallback = lookup_token_rule(model, provider)
    tokenizer_id = rule.tokenizer_id
    if rule.dynamic_deepseek:
        tokenizer_id = resolve_deepseek_id(model)

    return TokenizerSpec(
        rule_id=rule.id,
        backend_kind=rule.backend,
        encoding=rule.encoding,
        tokenizer_id=tokenizer_id,
        fallback=is_fallback,
        warn=rule.warn if not is_fallback else GLOBAL_FALLBACK.warn,
    )
