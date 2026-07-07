"""多模型 Token 计数：按 provider/model 选择 tokenizer backend。"""

from __future__ import annotations

import logging
import os
from typing import Protocol

log = logging.getLogger(__name__)

# DeepSeek API / FixLoop 默认模型共用 DeepSeek 词表（可用环境变量覆盖）
DEFAULT_DEEPSEEK_TOKENIZER = "deepseek-ai/deepseek-llm-7b-chat"
DEEPSEEK_TOKENIZER_ENV = "DEEPSEEK_TOKENIZER_ID"

_COUNTER_CACHE: dict[tuple[str, str], TokenCounter] = {}


class TokenCounter(Protocol):
    """统一 count/fit 接口（tiktoken 或 HuggingFace tokenizers）。"""

    backend: str

    def count(self, text: str) -> int: ...

    def fit(self, text: str, limit: int) -> str: ...


class TiktokenCounter:
    """OpenAI tiktoken backend（GPT 系及未知模型 fallback）。"""

    def __init__(self, model: str = "gpt-4", *, encoding_name: str | None = None):
        import tiktoken

        if encoding_name:
            self.encoder = tiktoken.get_encoding(encoding_name)
            self.backend = f"tiktoken:{encoding_name}"
        else:
            try:
                self.encoder = tiktoken.encoding_for_model(model)
                self.backend = f"tiktoken:{model}"
            except KeyError:
                self.encoder = tiktoken.get_encoding("cl100k_base")
                self.backend = "tiktoken:cl100k_base"

    def count(self, text: str) -> int:
        return len(self.encoder.encode(text))

    def fit(self, text: str, limit: int) -> str:
        tokens = self.encoder.encode(text)
        if len(tokens) <= limit:
            return text
        return self.encoder.decode(tokens[:limit])


class HuggingFaceTokenizerCounter:
    """HuggingFace `tokenizers` backend（DeepSeek 等）。"""

    def __init__(self, tokenizer_id: str):
        self.tokenizer_id = tokenizer_id
        self._tokenizer = None
        self._fallback: TiktokenCounter | None = None
        self.backend = f"huggingface:{tokenizer_id}"
        self._load()

    def _load(self) -> None:
        try:
            from tokenizers import Tokenizer

            self._tokenizer = Tokenizer.from_pretrained(self.tokenizer_id)
        except Exception as exc:
            log.warning(
                "无法加载 HuggingFace tokenizer %s（%s），回退 cl100k_base",
                self.tokenizer_id,
                exc,
            )
            self._fallback = TiktokenCounter(encoding_name="cl100k_base")
            self.backend = "tiktoken:cl100k_base"

    def count(self, text: str) -> int:
        if self._fallback is not None:
            return self._fallback.count(text)
        assert self._tokenizer is not None
        return len(self._tokenizer.encode(text).ids)

    def fit(self, text: str, limit: int) -> str:
        if self._fallback is not None:
            return self._fallback.fit(text, limit)
        assert self._tokenizer is not None
        encoded = self._tokenizer.encode(text)
        if len(encoded.ids) <= limit:
            return text
        return self._tokenizer.decode(encoded.ids[:limit])


def resolve_deepseek_tokenizer_id(model: str = "") -> str:
    """解析 DeepSeek 模型名 → HuggingFace tokenizer repo id。"""
    override = os.environ.get(DEEPSEEK_TOKENIZER_ENV, "").strip()
    if override:
        return override

    model_lower = model.lower()
    if "coder" in model_lower:
        return "deepseek-ai/deepseek-coder-6.7b-base"
    if "v3" in model_lower or "r1" in model_lower:
        return "deepseek-ai/DeepSeek-V3"
    return DEFAULT_DEEPSEEK_TOKENIZER


def _build_counter(model: str, provider: str) -> TokenCounter:
    model_lower = (model or "").lower()
    provider_lower = (provider or "").lower()

    if provider_lower == "openai" or model_lower.startswith("gpt-"):
        return TiktokenCounter(model or "gpt-4")

    if provider_lower in ("deepseek", "anthropic_compat", "") or model_lower.startswith("deepseek"):
        tok_id = resolve_deepseek_tokenizer_id(model)
        return HuggingFaceTokenizerCounter(tok_id)

    if provider_lower == "ollama":
        if "qwen" in model_lower:
            return HuggingFaceTokenizerCounter("Qwen/Qwen2.5-0.5B-Instruct")
        return TiktokenCounter(model or "gpt-4")

    return TiktokenCounter(model or "gpt-4")


def resolve_token_counter(model: str = "", provider: str = "") -> TokenCounter:
    """按 model + provider 返回缓存的 TokenCounter 实例。"""
    key = (model.lower(), provider.lower())
    if key not in _COUNTER_CACHE:
        _COUNTER_CACHE[key] = _build_counter(model, provider)
    return _COUNTER_CACHE[key]


def clear_token_counter_cache() -> None:
    """测试用：清空 counter 缓存。"""
    _COUNTER_CACHE.clear()
