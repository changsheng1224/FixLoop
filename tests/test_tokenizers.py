"""多 tokenizer backend 单测。"""

import pytest

from agent_runtime.config import AgentConfig
from agent_runtime.context_manager import TokenBudget, fit_prompt_to_budget, fit_repair_user_prompt
from agent_runtime.providers.clients import FakeModelClient
from agent_runtime.runtime import Agent
from agent_runtime.tokenizer_registry import lookup_token_rule
from agent_runtime.tokenizers import (
    TiktokenCounter,
    clear_token_counter_cache,
    resolve_deepseek_tokenizer_id,
    resolve_token_counter,
    resolve_tokenizer_spec,
)
from agent_runtime.workspace import WorkspaceContext


@pytest.fixture(autouse=True)
def _clear_counter_cache():
    clear_token_counter_cache()
    yield
    clear_token_counter_cache()


def _is_tiktoken_backend(backend: str) -> bool:
    return backend.startswith("tiktoken:") or backend.startswith("tiktoken-local:")


class TestTokenizerRegistry:
    def test_exact_gpt4o_rule(self):
        rule, fallback = lookup_token_rule("gpt-4o", "fake")
        assert rule.id == "openai-gpt4o"
        assert not fallback

    def test_prefix_gpt_rule(self):
        rule, fallback = lookup_token_rule("gpt-4-turbo", "fake")
        assert rule.id == "openai-gpt-prefix"
        assert not fallback

    def test_deepseek_model_rule(self):
        rule, fallback = lookup_token_rule("deepseek-v4-pro", "fake")
        assert rule.id == "deepseek-model"
        assert not fallback

    def test_claude_approximate_rule(self):
        rule, fallback = lookup_token_rule("claude-3-5-sonnet", "anthropic")
        assert rule.id == "claude-prefix"
        assert rule.warn == "approximate_tokenizer"
        assert not fallback

    def test_unknown_global_fallback(self):
        rule, fallback = lookup_token_rule("some-model", "fake")
        assert rule.id == "global-fallback"
        assert fallback


class TestResolveTokenCounter:
    def test_cl100k_uses_packaged_asset_without_tiktoken_download(self, monkeypatch):
        import tiktoken

        def fail_get_encoding(*args, **kwargs):
            raise RuntimeError("network access should not be used")

        monkeypatch.setattr(tiktoken, "get_encoding", fail_get_encoding)
        counter = TiktokenCounter(encoding_name="cl100k_base")

        assert counter.backend == "tiktoken-local:cl100k_base"
        assert counter.count("Hello world") == 2

    def test_gpt_model_uses_packaged_cl100k_without_tiktoken_download(self, monkeypatch):
        import tiktoken

        def fail_download_path(*args, **kwargs):
            raise RuntimeError("network access should not be used")

        monkeypatch.setattr(tiktoken, "encoding_for_model", fail_download_path)
        monkeypatch.setattr(tiktoken, "get_encoding", fail_download_path)
        counter = TiktokenCounter(model="gpt-4")

        assert counter.backend == "tiktoken-local:cl100k_base"
        assert counter.count("Hello world") == 2

    def test_deepseek_default_uses_huggingface(self):
        counter = resolve_token_counter("deepseek-v4-pro", "deepseek")
        assert "huggingface" in counter.backend

    def test_openai_uses_tiktoken(self):
        counter = resolve_token_counter("gpt-4", "openai")
        assert _is_tiktoken_backend(counter.backend)

    def test_unknown_provider_with_deepseek_model_uses_huggingface(self):
        counter = resolve_token_counter("deepseek-v4-pro", "fake")
        assert "huggingface" in counter.backend

    def test_unknown_provider_and_model_falls_back_to_tiktoken(self):
        counter = resolve_token_counter("some-model", "fake")
        assert _is_tiktoken_backend(counter.backend)

    def test_tiktoken_load_failure_uses_offline_approximation(self, monkeypatch):
        import tiktoken

        import agent_runtime.tokenizers as tokenizers_mod

        def fail_load(*args, **kwargs):
            raise RuntimeError("network unavailable")

        monkeypatch.setattr(tokenizers_mod, "load_local_tiktoken_encoding", lambda _name: None)
        monkeypatch.setattr(tiktoken, "get_encoding", fail_load)
        counter = resolve_token_counter("gpt-4o", "openai")

        assert _is_tiktoken_backend(counter.backend)
        assert "approx" in counter.backend
        assert counter.count("你好 hello") > 0

    def test_unknown_model_sets_fallback_spec(self):
        spec = resolve_tokenizer_spec("some-model", "fake")
        assert spec.fallback is True
        assert spec.rule_id == "global-fallback"

    def test_unknown_model_logs_warning(self, monkeypatch):
        import agent_runtime.tokenizers as tokenizers_mod

        warnings: list[str] = []
        monkeypatch.setattr(
            tokenizers_mod.log,
            "warning",
            lambda msg, *args, **kwargs: warnings.append(msg % args if args else str(msg)),
        )
        clear_token_counter_cache()
        resolve_token_counter("caplog-unknown-model-xyz", "fake-caplog")
        assert any("fallback cl100k_base" in w for w in warnings)

    def test_claude_logs_approximate_warning(self, monkeypatch):
        import agent_runtime.tokenizers as tokenizers_mod

        warnings: list[str] = []
        monkeypatch.setattr(
            tokenizers_mod.log,
            "warning",
            lambda msg, *args, **kwargs: warnings.append(msg % args if args else str(msg)),
        )
        clear_token_counter_cache()
        resolve_token_counter("claude-3-opus-caplog-xyz", "anthropic")
        assert any("approximate_tokenizer" in w for w in warnings)

    def test_deepseek_chinese_counts_fewer_than_cl100k(self):
        text = "你好世界 hello"
        deepseek = resolve_token_counter("deepseek-v4-pro", "deepseek")
        cl100k = resolve_token_counter("gpt-4", "openai")
        assert deepseek.count(text) < cl100k.count(text)

    def test_env_override_tokenizer_id(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_TOKENIZER_ID", "deepseek-ai/deepseek-coder-6.7b-base")
        assert (
            resolve_deepseek_tokenizer_id("deepseek-v4-pro")
            == "deepseek-ai/deepseek-coder-6.7b-base"
        )


class TestResolveDeepseekTokenizerId:
    def test_coder_model(self):
        assert "coder" in resolve_deepseek_tokenizer_id("deepseek-coder-v2")

    def test_v3_model(self):
        assert "V3" in resolve_deepseek_tokenizer_id("deepseek-v3")

    def test_default_model(self):
        assert (
            resolve_deepseek_tokenizer_id("deepseek-v4-pro") == "deepseek-ai/deepseek-llm-7b-chat"
        )


class TestTokenBudgetIntegration:
    def test_default_budget_uses_deepseek_backend(self):
        budget = TokenBudget()
        assert "huggingface" in budget.backend
        assert budget.provider == "deepseek"
        assert budget.tokenizer_fallback is False
        assert budget.tokenizer_id is not None

    def test_fit_respects_limit(self):
        budget = TokenBudget(model="deepseek-v4-pro", provider="deepseek", total_limit=6000)
        text = "token " * 5000
        fitted = budget.fit(text, 50)
        assert budget.count(fitted) <= 50

    def test_fit_prompt_to_budget_records_backend(self):
        _, _, meta = fit_prompt_to_budget("sys", "user", provider="deepseek")
        assert "huggingface" in meta["tokenizer_backend"]
        assert meta["tokenizer_fallback"] is False
        assert meta["tokenizer_id"]

    def test_fit_prompt_unknown_model_records_fallback(self):
        _, _, meta = fit_prompt_to_budget("sys", "user", model="unknown-x", provider="fake")
        assert meta["tokenizer_fallback"] is True
        assert _is_tiktoken_backend(meta["tokenizer_backend"])


class TestFitRepairUserPrompt:
    def test_uses_agent_config(self, temp_workspace):
        config = AgentConfig(
            provider="openai",
            model="gpt-4",
            max_steps=1,
            prompt_budget=800,
        )
        ws = WorkspaceContext.build(str(temp_workspace))
        agent = Agent(config=config, model_client=FakeModelClient([]), workspace=ws)
        fitted, meta = fit_repair_user_prompt(agent, "word " * 5000)
        assert meta.get("request_preserved") is True
        assert len(fitted) == len("word " * 5000)
        assert _is_tiktoken_backend(meta["tokenizer_backend"])
