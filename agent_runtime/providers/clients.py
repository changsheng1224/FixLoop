"""模型客户端：FakeClient（测试用）+ AnthropicCompatibleClient（真实 API）。

纯 urllib 实现，零第三方 HTTP 库依赖。
"""

import json
import time
import urllib.error
import urllib.request


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
                f"FakeClient 输出序列已耗尽"
                f"（共 {len(self._outputs)} 个，已用 {self._index} 个）"
            )
        result = self._outputs[self._index]
        self._index += 1
        return result


class AnthropicCompatibleModelClient:
    """Anthropic Messages API 兼容客户端。

    用纯 urllib 向兼容 Anthropic Messages API 的服务端（如 DeepSeek）发请求。
    支持自动重试、超时控制、prompt cache key 透传。
    """

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        temperature: float = 0.2,
        timeout: int = 60,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.supports_prompt_cache = False  # M2 启用

    def complete(self, prompt: str, max_new_tokens: int = 512) -> str:
        """向模型 API 发送请求并返回文本。

        Args:
            prompt: 完整 prompt 文本。
            max_new_tokens: 最大生成 token 数。

        Returns:
            模型返回的文本内容。

        Raises:
            RuntimeError: 请求失败且重试耗尽。
        """
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                }
            ],
            "max_tokens": max_new_tokens,
            "temperature": self.temperature,
        }
        body = json.dumps(payload).encode("utf-8")

        last_error = None
        for attempt in range(3):
            try:
                request = urllib.request.Request(
                    f"{self.base_url}/messages",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                    },
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                return self._extract_text(data)

            except urllib.error.HTTPError as e:
                last_error = e
                status = e.code
                # 5xx 服务端错误 → 重试；4xx 客户端错误 → 不重试
                if status < 500:
                    raise RuntimeError(
                        f"API 请求失败 (HTTP {status}): {e.reason}"
                    ) from e
                if attempt < 2:
                    wait = (attempt + 1) * 2  # 2s, 4s 间隔
                    time.sleep(wait)

            except (urllib.error.URLError, OSError) as e:
                last_error = e
                if attempt < 2:
                    time.sleep((attempt + 1) * 2)

        raise RuntimeError(
            f"API 请求失败，已重试 3 次。最后错误: {last_error}"
        )

    def _extract_text(self, data: dict) -> str:
        """从 Anthropic Messages API 响应中提取文本。

        Args:
            data: API 返回的 JSON 字典。

        Returns:
            拼接后的文本内容。
        """
        # Anthropic 格式: {"content": [{"type": "text", "text": "..."}, ...]}
        content = data.get("content", [])
        if isinstance(content, list):
            parts = [
                item["text"] for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            if parts:
                return "".join(parts)
        elif isinstance(content, str):
            return content
        # 兼容 OpenAI 格式: {"choices": [{"message": {"content": "..."}}]}
        choices = data.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            return msg.get("content", "")
        return ""
