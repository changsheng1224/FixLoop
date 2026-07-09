"""多 tokenizer backend 单测。"""

import pytest

from agent_runtime.context_manager import TokenBudget, fit_prompt_to_budget
from agent_runtime.tokenizers import (
    clear_token_counter_cache,
    resolve_deepseek_tokenizer_id,
    resolve_token_counter,
)


@pytest.fixture(autouse=True)
def _clear_counter_cache():
    clear_token_counter_cache()
    yield
    clear_token_counter_cache()


class TestResolveTokenCounter:
    def test_deepseek_default_uses_huggingface(self):
        counter = resolve_token_counter("deepseek-v4-pro", "deepseek")
        assert "huggingface" in counter.backend

    def test_openai_uses_tiktoken(self):
        counter = resolve_token_counter("gpt-4", "openai")
        assert counter.backend.startswith("tiktoken:")

    def test_unknown_provider_with_deepseek_model_uses_huggingface(self):
        counter = resolve_token_counter("deepseek-v4-pro", "fake")
        assert "huggingface" in counter.backend

    def test_unknown_provider_and_model_falls_back_to_tiktoken(self):
        counter = resolve_token_counter("some-model", "fake")
        assert counter.backend.startswith("tiktoken:")

    def test_deepseek_chinese_counts_fewer_than_cl100k(self):
        text = "你好世界 hello"
        deepseek = resolve_token_counter("deepseek-v4-pro", "deepseek")
        cl100k = resolve_token_counter("gpt-4", "openai")
        assert deepseek.count(text) < cl100k.count(text)

    def test_env_override_tokenizer_id(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_TOKENIZER_ID", "deepseek-ai/deepseek-coder-6.7b-base")
        assert resolve_deepseek_tokenizer_id("deepseek-v4-pro") == "deepseek-ai/deepseek-coder-6.7b-base"


class TestResolveDeepseekTokenizerId:
    def test_coder_model(self):
        assert "coder" in resolve_deepseek_tokenizer_id("deepseek-coder-v2")

    def test_v3_model(self):
        assert "V3" in resolve_deepseek_tokenizer_id("deepseek-v3")

    def test_default_model(self):
        assert resolve_deepseek_tokenizer_id("deepseek-v4-pro") == "deepseek-ai/deepseek-llm-7b-chat"


class TestTokenBudgetIntegration:
    def test_default_budget_uses_deepseek_backend(self):
        budget = TokenBudget()
        assert "huggingface" in budget.backend
        assert budget.provider == "deepseek"

    def test_fit_respects_limit(self):
        budget = TokenBudget(model="deepseek-v4-pro", provider="deepseek", total_limit=6000)
        text = "token " * 5000
        fitted = budget.fit(text, 50)
        assert budget.count(fitted) <= 50

    def test_fit_prompt_to_budget_records_backend(self):
        _, _, meta = fit_prompt_to_budget("sys", "user", provider="deepseek")
        assert "huggingface" in meta["tokenizer_backend"]
