"""多模型 Token 计数：按 provider/model 选择 tokenizer backend。"""

from __future__ import annotations

import os
from typing import Protocol

from agent_runtime.logging_setup import get_logger
from agent_runtime.tiktoken_assets import load_local_tiktoken_encoding
from agent_runtime.tokenizer_assets import local_tokenizer_json
from agent_runtime.tokenizer_registry import TokenizerSpec, build_tokenizer_spec

log = get_logger("tokenizers")

# DeepSeek API / FixLoop 默认模型共用 DeepSeek 词表（可用环境变量覆盖）
DEFAULT_DEEPSEEK_TOKENIZER = "deepseek-ai/deepseek-llm-7b-chat"
DEEPSEEK_TOKENIZER_ENV = "DEEPSEEK_TOKENIZER_ID"

_COUNTER_CACHE: dict[tuple[str, str], TokenCounter] = {}
_SPEC_CACHE: dict[tuple[str, str], TokenizerSpec] = {}
_WARNED_KEYS: set[tuple[str, str]] = set()


class TokenCounter(Protocol):
    """统一 count/fit 接口（tiktoken 或 HuggingFace tokenizers）。"""

    backend: str

    def count(self, text: str) -> int: ...

    def fit(self, text: str, limit: int) -> str: ...


class ApproxTokenCounter:
    """Offline approximation used when tokenizer assets cannot be loaded."""

    def __init__(self, backend: str = "approx"):
        self.backend = backend

    def count(self, text: str) -> int:
        if not text:
            return 0
        total = 0
        ascii_run = 0

        def flush_ascii() -> None:
            nonlocal total, ascii_run
            if not ascii_run:
                return
            total += 1 if ascii_run <= 6 else max(1, (ascii_run + 3) // 4)
            ascii_run = 0

        for ch in text:
            if ch.isspace():
                flush_ascii()
                continue
            if ord(ch) > 127:
                flush_ascii()
                total += 1
            else:
                ascii_run += 1
        flush_ascii()
        return max(1, total)

    def fit(self, text: str, limit: int) -> str:
        if limit <= 0:
            return ""
        out = []
        for ch in text:
            candidate = "".join(out) + ch
            if self.count(candidate) > limit:
                break
            out.append(ch)
        return "".join(out)


class TiktokenCounter:
    """OpenAI tiktoken backend（GPT 系及未知模型 fallback）。"""

    def __init__(self, model: str = "gpt-4", *, encoding_name: str | None = None):
        self._fallback: ApproxTokenCounter | None = None
        try:
            import tiktoken

            if encoding_name:
                self.encoder = load_local_tiktoken_encoding(encoding_name)
                if self.encoder is not None:
                    self.backend = f"tiktoken-local:{encoding_name}"
                else:
                    self.encoder = tiktoken.get_encoding(encoding_name)
                    self.backend = f"tiktoken:{encoding_name}"
            else:
                try:
                    mapped_encoding = tiktoken.model.encoding_name_for_model(model)
                except KeyError:
                    mapped_encoding = "cl100k_base"

                self.encoder = load_local_tiktoken_encoding(mapped_encoding)
                if self.encoder is not None:
                    self.backend = f"tiktoken-local:{mapped_encoding}"
                else:
                    try:
                        self.encoder = tiktoken.encoding_for_model(model)
                        self.backend = f"tiktoken:{model}"
                    except KeyError:
                        self.encoder = tiktoken.get_encoding("cl100k_base")
                        self.backend = "tiktoken:cl100k_base"
        except Exception as exc:
            backend = f"tiktoken:{encoding_name or model}:approx"
            log.warning(
                "无法加载 tiktoken tokenizer %s（%s），使用近似计数", encoding_name or model, exc
            )
            self.encoder = None
            self._fallback = ApproxTokenCounter(backend)
            self.backend = backend

    def count(self, text: str) -> int:
        if self._fallback is not None:
            return self._fallback.count(text)
        return len(self.encoder.encode(text))

    def fit(self, text: str, limit: int) -> str:
        if self._fallback is not None:
            return self._fallback.fit(text, limit)
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

            local_json = local_tokenizer_json(self.tokenizer_id)
            if local_json is not None:
                self._tokenizer = Tokenizer.from_file(str(local_json))
                self.backend = f"huggingface-local:{self.tokenizer_id}"
            else:
                self._tokenizer = Tokenizer.from_pretrained(self.tokenizer_id)
                self.backend = f"huggingface:{self.tokenizer_id}"
        except Exception as exc:
            log.warning(
                "无法加载 HuggingFace tokenizer %s（%s），回退 cl100k_base",
                self.tokenizer_id,
                exc,
            )
            self._fallback = TiktokenCounter(encoding_name="cl100k_base")
            self.backend = self._fallback.backend

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


def resolve_tokenizer_spec(model: str = "", provider: str = "") -> TokenizerSpec:
    """按 model + provider 返回 tokenizer 解析规格（含 fallback/warn 标记）。"""
    key = (model.lower(), provider.lower())
    if key not in _SPEC_CACHE:
        _SPEC_CACHE[key] = build_tokenizer_spec(
            model,
            provider,
            resolve_deepseek_id=resolve_deepseek_tokenizer_id,
        )
    return _SPEC_CACHE[key]


def _maybe_warn_spec(model: str, provider: str, spec: TokenizerSpec) -> None:
    key = (model.lower(), provider.lower())
    if key in _WARNED_KEYS:
        return
    if spec.fallback:
        log.warning(
            "unknown model %r (provider=%r), fallback cl100k_base",
            model or "(empty)",
            provider or "(empty)",
        )
        _WARNED_KEYS.add(key)
        return
    if spec.warn:
        log.warning(
            "tokenizer %s for model %r (provider=%r): %s",
            spec.rule_id,
            model or "(empty)",
            provider or "(empty)",
            spec.warn,
        )
        _WARNED_KEYS.add(key)


def _build_counter(model: str, provider: str) -> TokenCounter:
    spec = resolve_tokenizer_spec(model, provider)
    _maybe_warn_spec(model, provider, spec)

    if spec.backend_kind == "huggingface":
        assert spec.tokenizer_id is not None
        return HuggingFaceTokenizerCounter(spec.tokenizer_id)

    if spec.encoding:
        return TiktokenCounter(model or "gpt-4", encoding_name=spec.encoding)
    return TiktokenCounter(model or "gpt-4")


def resolve_token_counter(model: str = "", provider: str = "") -> TokenCounter:
    """按 model + provider 返回缓存的 TokenCounter 实例。"""
    key = (model.lower(), provider.lower())
    if key not in _COUNTER_CACHE:
        _COUNTER_CACHE[key] = _build_counter(model, provider)
    return _COUNTER_CACHE[key]


def clear_token_counter_cache() -> None:
    """测试用：清空 counter 与 spec 缓存。"""
    _COUNTER_CACHE.clear()
    _SPEC_CACHE.clear()
    _WARNED_KEYS.clear()
