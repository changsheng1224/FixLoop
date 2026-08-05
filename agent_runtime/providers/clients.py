"""模型客户端：FakeClient（测试用）+ AnthropicCompatibleClient（真实 API）。

纯 urllib 实现，零第三方 HTTP 库依赖。
"""

import json
import time
import urllib.error
import urllib.request

from agent_runtime.errors import EmptyModelResponse
from agent_runtime.model_timing import ModelCallTiming
from agent_runtime.providers.http_timing import read_http_body_with_timing
from agent_runtime.providers.session_usage import SessionUsageMixin


def _check_empty_response(raw: str, model: str = "") -> None:
    """检查模型响应是否为空；空时抛出 EmptyModelResponse。"""
    if not (raw or "").strip():
        raise EmptyModelResponse(model=model, detail="模型返回空响应")


class FakeModelClient(SessionUsageMixin):
    """模拟模型客户端：预设输出序列，用于单元测试。

    不调真实 API，按顺序弹出预设的字符串。
    支持 prompts 列表记录所有收到的 prompt。
    """

    def __init__(self, outputs: list[str]):
        self._outputs = outputs
        self._index = 0
        self.supports_prompt_cache = False
        self.prompts: list[str] = []
        self.cache_keys: list[str] = []
        self._init_usage_tracking()

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
        self.cache_keys.append(prompt_cache_key)
        if self._index >= len(self._outputs):
            raise RuntimeError(
                f"FakeClient 输出序列已耗尽（共 {len(self._outputs)} 个，已用 {self._index} 个）"
            )
        result = self._outputs[self._index]
        self._index += 1
        self._record_usage(prompt, result)
        _check_empty_response(result)
        out_tokens = max(1, len(result) // 4)
        timing = ModelCallTiming(ttft_ms=0, total_ms=0, output_tokens=out_tokens)
        self.last_call_timing = timing
        self.last_call_timings = [timing]
        return result


class FakeNativeToolClient(FakeModelClient):
    """带 chat_with_tools 的 FakeClient，用于测试原生 tool API 路径。"""

    def chat_with_tools(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[dict],
        executor,
        max_turns: int = 6,
        phase_hook=None,
        step_boundary_hook=None,
        cancel_token=None,
    ) -> tuple[str, dict]:
        """模拟原生 tool 多轮对话（测试用）。"""
        import json
        import re

        from agent_runtime.cancellation import CancelledError

        call_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "calls": 0,
        }
        call_timings: list[ModelCallTiming] = []
        user_msg = user_message

        for turn in range(max_turns):
            if cancel_token is not None and cancel_token.is_cancelled:
                raise CancelledError(cancel_token.reason)
            step = turn + 1
            if phase_hook is not None:
                from agent_runtime.react_phases import ReactPhase

                phase_hook(ReactPhase.REASONING, step=step)
            full = f"{system_prompt}\n\n{user_msg}" if system_prompt else user_msg
            raw = self.complete(full)
            if step_boundary_hook is not None:
                step_boundary_hook(step)
            usage = dict(self.last_usage)
            call_usage["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
            call_usage["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
            call_usage["cache_read_tokens"] += int(usage.get("cache_read_tokens", 0) or 0)
            call_usage["cache_creation_tokens"] += int(usage.get("cache_creation_tokens", 0) or 0)
            call_usage["calls"] += 1
            if self.last_call_timing is not None:
                timing = ModelCallTiming(
                    ttft_ms=self.last_call_timing.ttft_ms,
                    total_ms=self.last_call_timing.total_ms,
                    output_tokens=self.last_call_timing.output_tokens,
                    step=turn + 1,
                )
                call_timings.append(timing)
                self.last_call_timing = timing

            final_match = re.search(r"<final>(.*?)</final>", raw, re.DOTALL)
            if final_match:
                answer = final_match.group(1).strip()
                self.last_call_usage = dict(call_usage)
                self.last_call_timings = call_timings
                return answer, call_usage

            tool_match = re.search(r"<tool>(.*?)</tool>", raw, re.DOTALL)
            if tool_match:
                try:
                    payload = json.loads(tool_match.group(1))
                except json.JSONDecodeError:
                    self.last_call_usage = dict(call_usage)
                    self.last_call_timings = call_timings
                    return raw.strip(), call_usage
                name = payload.get("name", "")
                args = payload.get("args", {})
                if phase_hook is not None:
                    from agent_runtime.react_phases import ReactPhase

                    phase_hook(ReactPhase.ACTING, step=step, tool=name)
                try:
                    result = executor(name, args)
                except Exception as e:
                    from agent_runtime.terminal_tool import TerminalToolAccepted

                    if isinstance(e, TerminalToolAccepted):
                        self.last_call_usage = dict(call_usage)
                        self.last_call_timings = call_timings
                        raise
                    raise
                if phase_hook is not None:
                    from agent_runtime.react_phases import ReactPhase

                    phase_hook(ReactPhase.OBSERVATION, step=step, tool=name)
                user_msg = f"工具 {name} 执行完成。\n结果:\n{result}"
                continue

            self.last_call_usage = dict(call_usage)
            self.last_call_timings = call_timings
            return raw.strip(), call_usage

        self.last_call_usage = dict(call_usage)
        self.last_call_timings = call_timings
        from agent_runtime.loop_limits import NATIVE_MAX_TURNS_MESSAGE

        return NATIVE_MAX_TURNS_MESSAGE, call_usage


class AnthropicCompatibleModelClient(SessionUsageMixin):
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
        self._init_usage_tracking()

    def _record_usage(self, usage: dict | None) -> None:
        from agent_runtime.token_accounting import parse_provider_usage

        if not usage:
            return
        parsed = parse_provider_usage(usage)
        inp = parsed["input_tokens"]
        out = parsed["output_tokens"]
        cache_read = parsed["cache_read_tokens"]
        cache_creation = parsed["cache_creation_tokens"]
        self.last_usage = {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": cache_creation,
        }
        self.session_usage["input_tokens"] += inp
        self.session_usage["output_tokens"] += out
        self.session_usage["cache_read_tokens"] = (
            int(self.session_usage.get("cache_read_tokens", 0) or 0) + cache_read
        )
        self.session_usage["cache_creation_tokens"] = (
            int(self.session_usage.get("cache_creation_tokens", 0) or 0) + cache_creation
        )
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
        phase_hook=None,
        step_boundary_hook=None,
        cancel_token=None,
    ) -> tuple[str, dict]:
        """使用原生 Anthropic tool_use 协议进行多轮对话。

        Args:
            system_prompt: 系统提示词。
            user_message: 用户消息。
            tools: 工具定义列表 [{"name":"...","description":"...","input_schema":{...}}]。
            executor: 工具执行回调 fn(name, args) -> str。
            max_turns: 最大对话轮数。

        Returns:
            (最终文本回复, 本次 call 累计 usage dict)。
        """
        from agent_runtime.cancellation import CancelledError

        messages = []
        call_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "calls": 0,
        }
        call_timings: list[ModelCallTiming] = []
        self.last_call_timings = []
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

        for turn in range(max_turns):
            if cancel_token is not None and cancel_token.is_cancelled:
                raise CancelledError(cancel_token.reason)
            step = turn + 1
            if phase_hook is not None:
                from agent_runtime.react_phases import ReactPhase

                phase_hook(ReactPhase.REASONING, step=step)
            payload = dict(payload_base)
            payload["messages"] = list(messages)  # shallow copy
            body = json.dumps(payload).encode("utf-8")
            data, timing = self._post_messages(body)
            if step_boundary_hook is not None:
                step_boundary_hook(step)
            timing.step = turn + 1
            call_timings.append(timing)
            self.last_call_timing = timing
            self.last_call_timings = list(call_timings)

            turn_usage = data.get("usage") or {}
            self._record_usage(turn_usage)
            from agent_runtime.token_accounting import parse_provider_usage

            parsed = parse_provider_usage(turn_usage)
            call_usage["input_tokens"] += parsed["input_tokens"]
            call_usage["output_tokens"] += parsed["output_tokens"]
            call_usage["cache_read_tokens"] += parsed["cache_read_tokens"]
            call_usage["cache_creation_tokens"] += parsed["cache_creation_tokens"]
            call_usage["calls"] += 1

            # 解析响应中的 content blocks
            content_blocks = data.get("content", [])
            if isinstance(content_blocks, str):
                self.last_call_usage = dict(call_usage)
                self.last_call_timings = call_timings
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
                    if phase_hook is not None:
                        from agent_runtime.react_phases import ReactPhase

                        phase_hook(ReactPhase.ACTING, step=step, tool=name)
                    try:
                        result = executor(name, inp)
                    except Exception as e:
                        from agent_runtime.terminal_tool import TerminalToolAccepted

                        if isinstance(e, TerminalToolAccepted):
                            self._save_request(full_text, e.payload)
                            self.last_call_usage = dict(call_usage)
                            self.last_call_timings = call_timings
                            raise
                        result = f"Error: {e}"
                    if phase_hook is not None:
                        from agent_runtime.react_phases import ReactPhase

                        phase_hook(ReactPhase.OBSERVATION, step=step, tool=name)
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
                answer = "".join(text_parts)
                self._save_request(full_text, answer)
                self.last_call_usage = dict(call_usage)
                self.last_call_timings = call_timings
                return answer, call_usage

            # 既没有文本也没有工具调用 → 空响应
            self.last_call_usage = dict(call_usage)
            self.last_call_timings = call_timings
            return "", call_usage

        self.last_call_usage = dict(call_usage)
        self.last_call_timings = call_timings
        from agent_runtime.loop_limits import NATIVE_MAX_TURNS_MESSAGE

        return NATIVE_MAX_TURNS_MESSAGE, call_usage

    def _post_messages(self, body: bytes) -> tuple[dict, ModelCallTiming]:
        """POST /messages with retries; return parsed JSON and TTFB timing."""
        from agent_runtime.logging_setup import get_logger
        from agent_runtime.providers.retry_policy import (
            RateLimitExceededError,
            compute_rate_limit_delay,
            compute_server_error_delay,
            parse_retry_after,
        )

        log = get_logger("model_client")
        last_error = None
        max_retries = 3

        for attempt in range(max_retries):
            try:
                request = urllib.request.Request(
                    f"{self.base_url}/messages",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "Connection": "keep-alive",
                    },
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw_bytes, ttft_ms, total_ms = read_http_body_with_timing(response)
                raw_text = raw_bytes.decode("utf-8")
                _check_empty_response(raw_text, model=self.model)
                data = json.loads(raw_text)
                from agent_runtime.token_accounting import parse_provider_usage

                parsed = parse_provider_usage(data.get("usage"))
                timing = ModelCallTiming(
                    ttft_ms=ttft_ms,
                    total_ms=total_ms,
                    output_tokens=parsed["output_tokens"],
                )
                self.last_call_timing = timing
                self._latencies.append(total_ms / 1000.0)
                return data, timing

            except urllib.error.HTTPError as e:
                last_error = e
                if e.code == 429:
                    if attempt < max_retries - 1:
                        retry_after = parse_retry_after(e.headers)
                        delay = compute_rate_limit_delay(attempt, retry_after)
                        log.debug(
                            "rate_limit_retry attempt=%s delay_s=%.2f retry_after=%s",
                            attempt + 1,
                            delay,
                            retry_after,
                        )
                        time.sleep(delay)
                        continue
                    raise RateLimitExceededError(
                        f"API 限流 (HTTP 429)，已重试 {max_retries} 次"
                    ) from e
                if e.code < 500:
                    raise RuntimeError(f"API 请求失败 (HTTP {e.code}): {e.reason}") from e
                if attempt < max_retries - 1:
                    delay = compute_server_error_delay(attempt)
                    log.debug(
                        "server_error_retry attempt=%s delay_s=%.2f code=%s",
                        attempt + 1,
                        delay,
                        e.code,
                    )
                    time.sleep(delay)
                    continue

            except (urllib.error.URLError, OSError) as e:
                last_error = e
                if attempt < max_retries - 1:
                    delay = compute_server_error_delay(attempt)
                    log.debug(
                        "network_retry attempt=%s delay_s=%.2f error=%s",
                        attempt + 1,
                        delay,
                        e,
                    )
                    time.sleep(delay)
                    continue

        raise RuntimeError(f"API 请求失败，已重试 {max_retries} 次。最后错误: {last_error}")

    def _call_api(self, payload: dict, prompt_for_log: str = "") -> str:
        """发送 API 请求并返回文本（单轮，无工具）。"""
        body = json.dumps(payload).encode("utf-8")
        data, timing = self._post_messages(body)
        self.last_call_timings = [timing]
        self._record_usage(data.get("usage"))
        result = self._extract_text(data)
        self._save_request(prompt_for_log, result)
        return result

    def latency_stats(self) -> dict:
        """返回响应延迟统计（秒）。"""
        from agent_runtime.model_timing import percentile_values

        if not self._latencies:
            return {"count": 0, "avg": 0, "p50": 0, "p99": 0}
        sorted_l = sorted(self._latencies)
        n = len(sorted_l)
        return {
            "count": n,
            "avg": round(sum(sorted_l) / n, 2),
            "p50": round(float(percentile_values(sorted_l, 0.5)), 2),
            "p99": round(float(percentile_values(sorted_l, 0.99)), 2),
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
        result = data.get("response", "")
        _check_empty_response(result, model=self.model)
        return result

    def complete_stream(
        self,
        prompt: str,
        *,
        max_new_tokens: int = 512,
        cancel_token=None,
        on_chunk=None,
    ) -> str:
        """Ollama 流式 generate；chunk 循环内检查 cancel_token。"""
        from agent_runtime.cancellation import CancelledError

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": True,
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
        parts: list[str] = []
        with urllib.request.urlopen(request, timeout=self.timeout) as resp:
            for raw_line in resp:
                if cancel_token is not None and cancel_token.is_cancelled:
                    break
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                data = json.loads(line)
                chunk = data.get("response", "")
                if chunk:
                    parts.append(chunk)
                    if on_chunk is not None:
                        on_chunk(chunk)
                if data.get("done"):
                    break
        if cancel_token is not None and cancel_token.is_cancelled:
            raise CancelledError(cancel_token.reason)
        return "".join(parts)


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
        payload = self._build_payload(prompt, max_new_tokens, stream=False)
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
        result = self._extract_text(data)
        _check_empty_response(result, model=self.model)
        return result

    def complete_stream(
        self, prompt: str, max_new_tokens: int = 512, on_chunk=None, cancel_token=None
    ):
        """OpenAI Responses API 流式调用。"""
        from agent_runtime.cancellation import CancelledError

        payload = self._build_payload(prompt, max_new_tokens, stream=True)
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/responses",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        parts: list[str] = []
        with urllib.request.urlopen(request, timeout=self.timeout) as resp:
            for line_bytes in resp:
                if cancel_token is not None and cancel_token.is_cancelled:
                    break
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    event = json.loads(data_str)
                    delta = _extract_stream_delta(event)
                    if delta:
                        parts.append(delta)
                        if on_chunk is not None:
                            on_chunk(delta)
                except json.JSONDecodeError:
                    pass
        if cancel_token is not None and cancel_token.is_cancelled:
            raise CancelledError(cancel_token.reason)
        full_text = "".join(parts)
        if not full_text:
            full_text = self._extract_text(json.loads("{}"))
        return full_text

    def _build_payload(self, prompt, max_new_tokens, stream):
        return {
            "model": self.model,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            "max_output_tokens": max_new_tokens,
            "temperature": self.temperature,
            "stream": stream,
        }

    @staticmethod
    def _extract_text(data):
        output = data.get("output", [])
        for item in output:
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        return c.get("text", "")
        return ""


def _extract_stream_delta(event: dict) -> str:
    """从 OpenAI SSE 事件中提取文本增量。"""
    if event.get("type") == "response.output_text.delta":
        return event.get("delta", "")
    output = event.get("output", [])
    for item in output:
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    return c.get("text", "")
    return ""
