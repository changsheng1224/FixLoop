"""模型后端适配器：抹平不同 Provider 的 HTTP 差异。"""

from agent_runtime.providers.clients import (
    AnthropicCompatibleModelClient,
    FakeModelClient,
)

__all__ = ["FakeModelClient", "AnthropicCompatibleModelClient"]
