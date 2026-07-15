"""L1 bootstrap：.env 加载与 ModelClient 装配（CLI 与 L2 共用）。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic/v1"
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"

_FAKE_DEFAULT_OUTPUT = "<final>FakeClient 未预设输出，请指定 --provider 为真实 Provider。</final>"


def load_dotenv(cwd: Path | None = None) -> None:
    """从 cwd/.env 加载 KEY=VAL 到 os.environ（不覆盖已有变量）。"""
    env_path = (cwd or Path.cwd()) / ".env"
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key and key not in os.environ:
                os.environ[key] = val


def create_model_client(
    model_client: Any | None = None,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    temperature: float | None = None,
    provider: str = "anthropic_compat",
) -> Any:
    """返回传入 client，或从环境变量 / 参数构造 ModelClient。"""
    if model_client is not None:
        return model_client

    load_dotenv()

    if provider == "fake":
        from agent_runtime.providers.clients import FakeModelClient

        return FakeModelClient([_FAKE_DEFAULT_OUTPUT])

    if provider == "ollama":
        from agent_runtime.providers.clients import OllamaModelClient

        kwargs: dict[str, Any] = {
            "model": model or os.environ.get("OLLAMA_MODEL", "qwen3.5:9b"),
            "host": base_url or os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL),
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        return OllamaModelClient(**kwargs)

    if provider == "openai":
        from agent_runtime.providers.clients import OpenAICompatibleModelClient

        kwargs: dict[str, Any] = {
            "model": model or os.environ.get("OPENAI_MODEL", "gpt-4o"),
            "base_url": base_url or os.environ.get("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL),
            "api_key": api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", ""),
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        return OpenAICompatibleModelClient(**kwargs)

    from agent_runtime.providers.clients import AnthropicCompatibleModelClient

    kwargs: dict[str, Any] = {
        "model": model or os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
        "base_url": base_url or os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
        "api_key": api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", ""),
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    return AnthropicCompatibleModelClient(**kwargs)
