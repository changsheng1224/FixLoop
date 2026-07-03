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
        self.last_usage: dict = {}
        self.session_usage: dict = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    def reset_session_usage(self) -> None:
        self.last_usage = {}
        self.session_usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    def _record_usage(self, prompt: str, result: str) -> None:
        inp = max(1, len(prompt) // 4)
        out = max(1, len(result) // 4)
        self.last_usage = {"input_tokens": inp, "output_tokens": out}
        self.session_usage["input_tokens"] += inp
        self.session_usage["output_tokens"] += out
        self.session_usage["calls"] += 1

    def complete(self, prompt: str, max_new_tokens: int = 512, prompt_cache_key: str = "") -> str:
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
        self._record_usage(prompt, result)
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
        self.supports_prompt_cache = True
        self._latencies: list[float] = []
        self.last_usage: dict = {}
        self.session_usage: dict = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    def reset_session_usage(self) -> None:
        self.last_usage = {}
        self.session_usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    def _record_usage(self, usage: dict | None) -> None:
        if not usage:
            return
        inp = int(usage.get("input_tokens", 0) or 0)
        out = int(usage.get("output_tokens", 0) or 0)
        self.last_usage = {"input_tokens": inp, "output_tokens": out}
        self.session_usage["input_tokens"] += inp
        self.session_usage["output_tokens"] += out
        self.session_usage["calls"] += 1

    def complete(
        self,
        prompt: str,
        max_new_tokens: int = 512,
        prompt_cache_key: str = "",
    ) -> str:
        """向模型 API 发送请求（无工具调用的简单模式）。

        Args:
            prompt: 完整 prompt 文本。
            max_new_tokens: 最大生成 token 数。
            prompt_cache_key: 可缓存的 prefix hash。
        """
        content = [{"type": "text", "text": prompt}]
        if prompt_cache_key and self.supports_prompt_cache:
            content[0]["cache_control"] = {"type": "ephemeral"}

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_new_tokens,
            "temperature": self.temperature,
        }
        return self._call_api(payload, prompt)

    def chat_with_tools(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[dict],
        executor,
        max_turns: int = 6,
    ) -> str:
        """使用原生 Anthropic tool_use 协议进行多轮对话。

        Args:
            system_prompt: 系统提示词。
            user_message: 用户消息。
            tools: 工具定义列表 [{"name":"...","description":"...","input_schema":{...}}]。
            executor: 工具执行回调 fn(name, args) -> str。
            max_turns: 最大对话轮数。

        Returns:
            模型的最终文本回复。
        """
        messages = []
        # 系统提示词放在第一条消息中
        full_text = system_prompt + "\n\n" + user_message if system_prompt else user_message
        messages.append(
            {
                "role": "user",
                "content": [{"type": "text", "text": full_text}],
            }
        )

        payload_base = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 4096,
            "temperature": self.temperature,
            "tools": tools,
        }

        for _ in range(max_turns):
            payload = dict(payload_base)
            payload["messages"] = list(messages)  # shallow copy
            body = json.dumps(payload).encode("utf-8")
            data = None

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
                    with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    break
                except urllib.error.HTTPError as e:
                    if e.code >= 500 and attempt < 2:
                        time.sleep((attempt + 1) * 2)
                        continue
                    raise RuntimeError(f"API 请求失败 (HTTP {e.code})") from e
                except (urllib.error.URLError, OSError) as e:
                    if attempt < 2:
                        time.sleep((attempt + 1) * 2)
                        continue
                    raise RuntimeError("API 请求失败") from e

            if data is None:
                raise RuntimeError("API 请求失败，已重试 3 次")

            self._record_usage(data.get("usage"))

            # 解析响应中的 content blocks
            content_blocks = data.get("content", [])
            if isinstance(content_blocks, str):
                return content_blocks

            # 收集所有 content blocks
            text_parts = []
            tool_uses = []

            for block in content_blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_uses.append(block)

            # 将模型的回复加入 messages
            messages.append({"role": "assistant", "content": content_blocks})

            # 如果有工具调用，执行并继续
            if tool_uses:
                tool_results = []
                for tu in tool_uses:
                    name = tu.get("name", "")
                    inp = tu.get("input", {})
                    tu_id = tu.get("id", "")
                    try:
                        result = executor(name, inp)
                    except Exception as e:
                        result = f"Error: {e}"
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu_id,
                            "content": str(result),
                        }
                    )
                messages.append({"role": "user", "content": tool_results})
                continue  # 继续下一轮

            # 没有工具调用 → 返回文本
            if text_parts:
                self._save_request(full_text, "".join(text_parts))
                return "".join(text_parts)

            # 既没有文本也没有工具调用 → 空响应
            return ""

        return "max_turns exceeded"

    def _call_api(self, payload: dict, prompt_for_log: str = "") -> str:
        """发送 API 请求并返回文本（单轮，无工具）。"""
        body = json.dumps(payload).encode("utf-8")
        t0 = time.time()
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
                self._record_usage(data.get("usage"))
                result = self._extract_text(data)
                self._save_request(prompt_for_log, result)
                self._latencies.append(time.time() - t0)
                return result

            except urllib.error.HTTPError as e:
                last_error = e
                if e.code < 500:
                    raise RuntimeError(f"API 请求失败 (HTTP {e.code}): {e.reason}") from e
                if attempt < 2:
                    time.sleep((attempt + 1) * 2)

            except (urllib.error.URLError, OSError) as e:
                last_error = e
                if attempt < 2:
                    time.sleep((attempt + 1) * 2)

        raise RuntimeError(f"API 请求失败，已重试 3 次。最后错误: {last_error}")

    def latency_stats(self) -> dict:
        """返回响应延迟统计（秒）。"""
        if not self._latencies:
            return {"count": 0, "avg": 0, "p50": 0, "p99": 0}
        sorted_l = sorted(self._latencies)
        n = len(sorted_l)
        return {
            "count": n,
            "avg": round(sum(sorted_l) / n, 2),
            "p50": round(sorted_l[n // 2], 2),
            "p99": round(sorted_l[int(n * 0.99)], 2),
        }

    def _save_request(self, prompt: str, result: str):
        """记录最后一次请求到 .agent/last_request.json（调试用）。"""
        try:
            from pathlib import Path

            agent_dir = Path.cwd() / ".agent"
            agent_dir.mkdir(parents=True, exist_ok=True)
            path = agent_dir / "last_request.json"
            path.write_text(
                json.dumps(
                    {
                        "model": self.model,
                        "prompt_preview": prompt[:500],
                        "prompt_length": len(prompt),
                        "result_preview": result[:300],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

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
                item["text"]
                for item in content
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


class OllamaModelClient:
    """Ollama 本地模型客户端。

    纯 urllib 实现，向 Ollama REST API（默认 http://127.0.0.1:11434）发请求。
    """

    def __init__(
        self,
        model: str = "qwen3.5:9b",
        host: str = "http://127.0.0.1:11434",
        temperature: float = 0.2,
        top_p: float = 0.9,
        timeout: int = 120,
    ):
        self.model = model
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout
        self.supports_prompt_cache = False

    def complete(self, prompt: str, max_new_tokens: int = 512, prompt_cache_key: str = "") -> str:
        """调用 Ollama /api/generate 并返回文本。"""
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": max_new_tokens,
                "temperature": self.temperature,
                "top_p": self.top_p,
            },
        }
        body = json.dumps(payload).encode("utf-8")

        request = urllib.request.Request(
            f"{self.host}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("response", "")


class OpenAICompatibleModelClient:
    """OpenAI Responses API 兼容客户端。

    支持 `/v1/responses` 端点，支持 SSE 流解析和 usage 提取。
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        temperature: float = 0.2,
        timeout: int = 60,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.supports_prompt_cache = False

    def complete(self, prompt: str, max_new_tokens: int = 512, prompt_cache_key: str = "") -> str:
        """调用 OpenAI Responses API。"""
        payload = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ],
            "max_output_tokens": max_new_tokens,
            "temperature": self.temperature,
        }
        body = json.dumps(payload).encode("utf-8")

        request = urllib.request.Request(
            f"{self.base_url}/responses",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        # 提取文本
        output = data.get("output", [])
        for item in output:
            if item.get("type") == "message":
                content_list = item.get("content", [])
                for c in content_list:
                    if c.get("type") == "output_text":
                        return c.get("text", "")
        return ""
