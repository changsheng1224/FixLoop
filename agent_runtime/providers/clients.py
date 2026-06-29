"""模型客户端：FakeClient（测试用）+ AnthropicCompatibleClient（真实 API）。

纯 urllib 实现，零第三方 HTTP 库依赖。
"""


class FakeModelClient:
    """模拟模型客户端：预设输出序列，用于单元测试。

    不调真实 API，按顺序弹出预设的字符串。
    支持 prompts 列表记录所有收到的 prompt。
    """

    def __init__(self, outputs: list[str]):
        self._outputs = outputs
        self._index = 0
        self.supports_prompt_cache = False
        self.prompts: list[str] = []

    def complete(self, prompt: str, max_new_tokens: int = 512) -> str:
        """弹出下一个预设输出。

        Args:
            prompt: 完整 prompt 文本（记录但不影响返回值）。
            max_new_tokens: 最大 token 数（保留参数，FakeClient 忽略）。

        Returns:
            预设的输出字符串。

        Raises:
            RuntimeError: 输出序列已耗尽。
        """
        self.prompts.append(prompt)
        if self._index >= len(self._outputs):
            raise RuntimeError(
                f"FakeClient 输出序列已耗尽（共 {len(self._outputs)} 个，已用 {self._index} 个）"
            )
        result = self._outputs[self._index]
        self._index += 1
        return result


class AnthropicCompatibleModelClient:
    """Anthropic Messages API 兼容客户端（TODO: Day 2 实现）。"""

    pass
