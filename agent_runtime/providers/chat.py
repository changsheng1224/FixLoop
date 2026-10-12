"""Tool-free structured chat over the existing L1 provider configurations.

This path does not construct an Agent, scan a workspace, or save runtime memory.
Unlike the tool loop, ordinary chat accepts prose without a <final> wrapper.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator

from agent_runtime.cancellation import CancellationToken
from agent_runtime.model_turn import (
    FinishKind,
    ModelTurnRequest,
    ModelTurnResult,
    ProviderFinish,
    normalize_anthropic_finish,
    normalize_openai_finish,
)
from agent_runtime.providers.clients import (
    AnthropicCompatibleModelClient,
    FakeModelClient,
    OllamaModelClient,
    OpenAICompatibleModelClient,
)
from agent_runtime.providers.contracts import ProviderError, ProviderErrorCode


def _events(response, *, sse: bool) -> Iterator[dict]:
    """Read SSE frames (including multiline data) or Ollama NDJSON."""
    data: list[str] = []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not sse:
            if line.strip():
                yield json.loads(line)
            continue
        if not line:
            if data:
                payload = "\n".join(data)
                data.clear()
                if payload == "[DONE]":
                    return
                yield json.loads(payload)
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data and "\n".join(data) != "[DONE]":
        yield json.loads("\n".join(data))


def stream_chat_turn(
    client,
    request: ModelTurnRequest,
    *,
    cancel_token: CancellationToken,
    on_chunk: Callable[[str], None],
) -> ModelTurnResult:
    """Stream a tool-free turn, retaining roles and normalized finish/usage.

    No automatic retry after output: repeating a partial response could duplicate
    text or billing. The caller may regenerate on a new conversation branch.
    """
    cancel_token.check()
    if request.tools or request.tool_choice is not None:
        raise ValueError("ordinary chat cannot request tools")
    if isinstance(client, FakeModelClient):
        text = client.complete(
            json.dumps({"system": request.system_prompt, "messages": request.messages}),
            max_new_tokens=request.max_output_tokens,
        )
        if text.startswith("<final>") and text.endswith("</final>"):
            text = text[7:-8]
        cancel_token.check()
        on_chunk(text)
        return ModelTurnResult(
            text=text,
            finish=ProviderFinish(FinishKind.TEXT_COMPLETE, "stop", "fake"),
            usage=dict(client.last_usage),
        )

    headers = {"Content-Type": "application/json"}
    if isinstance(client, AnthropicCompatibleModelClient):
        provider = "anthropic"
        endpoint = client.base_url + "/messages"
        headers.update({"x-api-key": client.api_key, "anthropic-version": "2023-06-01"})
        payload = {
            "model": client.model,
            "system": request.system_prompt,
            "messages": request.messages,
            "max_tokens": request.max_output_tokens,
            "temperature": client.temperature,
            "stream": True,
        }
    elif isinstance(client, OpenAICompatibleModelClient):
        provider = "openai"
        endpoint = client.base_url + "/responses"
        headers["Authorization"] = "Bearer " + client.api_key
        payload = {
            "model": client.model,
            "instructions": request.system_prompt,
            "input": request.messages,
            "max_output_tokens": request.max_output_tokens,
            "temperature": client.temperature,
            "stream": True,
        }
    elif isinstance(client, OllamaModelClient):
        provider = "ollama"
        endpoint = client.host + "/api/chat"
        payload = {
            "model": client.model,
            "messages": [{"role": "system", "content": request.system_prompt}, *request.messages],
            "stream": True,
            "options": {
                "num_predict": request.max_output_tokens,
                "temperature": client.temperature,
                "top_p": client.top_p,
            },
        }
    else:
        raise ValueError("unsupported ordinary chat provider")

    http_request = urllib.request.Request(
        endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers
    )
    parts: list[str] = []
    usage: dict = {}
    reason = ""
    completed = False
    incomplete = False
    has_tools = False
    try:
        with urllib.request.urlopen(
            http_request, timeout=request.remaining_timeout(client.timeout)
        ) as response:
            for event in _events(response, sse=provider != "ollama"):
                cancel_token.check()
                if request.deadline is not None and time.monotonic() >= request.deadline:
                    raise TimeoutError("chat deadline exceeded")
                chunk = ""
                kind = event.get("type", "")
                if kind == "error" or event.get("error"):
                    raise ProviderError(ProviderErrorCode.PROTOCOL, "provider stream failed")
                if provider == "anthropic":
                    if kind == "message_start":
                        usage.update(event.get("message", {}).get("usage", {}))
                    elif kind == "content_block_start":
                        has_tools |= event.get("content_block", {}).get("type") == "tool_use"
                        block = event.get("content_block", {})
                        if block.get("type") == "text":
                            chunk = block.get("text", "")
                    elif kind == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            chunk = delta.get("text", "")
                    elif kind == "message_delta":
                        reason = event.get("delta", {}).get("stop_reason") or reason
                        usage.update(event.get("usage", {}))
                    elif kind == "message_stop":
                        completed = True
                elif provider == "openai":
                    if kind == "response.output_text.delta":
                        chunk = event.get("delta", "")
                    elif kind == "response.output_item.added":
                        has_tools |= event.get("item", {}).get("type") == "function_call"
                    elif kind in {"response.completed", "response.incomplete", "response.failed"}:
                        body = event.get("response", {})
                        incomplete = (
                            kind == "response.incomplete" or body.get("status") == "incomplete"
                        )
                        has_tools |= any(
                            item.get("type") == "function_call"
                            for item in body.get("output", [])
                            if isinstance(item, dict)
                        )
                        usage.update(body.get("usage") or {})
                        reason = (body.get("incomplete_details") or {}).get("reason", "")
                        reason = reason or body.get("status", "completed")
                        completed = kind != "response.failed"
                        if not completed:
                            raise ProviderError(
                                ProviderErrorCode.PROTOCOL, "provider response failed"
                            )
                else:
                    message = event.get("message", {})
                    has_tools |= bool(message.get("tool_calls"))
                    chunk = message.get("content", "")
                    if event.get("done"):
                        completed = True
                        reason = event.get("done_reason", "stop")
                        usage = {
                            "input_tokens": event.get("prompt_eval_count", 0),
                            "output_tokens": event.get("eval_count", 0),
                        }
                if chunk:
                    if not isinstance(chunk, str):
                        raise ValueError("provider text delta must be a string")
                    on_chunk(chunk)
                    parts.append(chunk)
                if completed:
                    break
    except urllib.error.HTTPError as exc:
        code = (
            ProviderErrorCode.AUTHENTICATION
            if exc.code in {401, 403}
            else (ProviderErrorCode.RATE_LIMIT if exc.code == 429 else ProviderErrorCode.PROTOCOL)
        )
        exc.close()
        raise ProviderError(code, f"provider HTTP {exc.code}", provider=provider) from exc
    cancel_token.check()
    text = "".join(parts)
    if not completed:
        raise ProviderError(ProviderErrorCode.PROTOCOL, "provider stream ended before completion")
    if has_tools:
        raise ProviderError(ProviderErrorCode.PROTOCOL, "ordinary chat received a tool call")
    finish = (
        normalize_anthropic_finish(reason, has_tools=False, has_text=bool(text.strip()))
        if provider == "anthropic"
        else normalize_openai_finish(reason, has_text=bool(text.strip()))
    )
    # A diagnostic reason must not override the provider's terminal status.
    if incomplete and finish == FinishKind.TEXT_COMPLETE:
        finish = FinishKind.PROVIDER_ERROR
    from agent_runtime.token_accounting import parse_provider_usage

    return ModelTurnResult(
        text=text,
        finish=ProviderFinish(finish, reason, provider),
        usage=parse_provider_usage(usage),
    )
