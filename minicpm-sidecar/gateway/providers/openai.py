"""OpenAI-compatible API provider.

Supports any OpenAI-compatible endpoint (OpenAI, DeepSeek, Together, etc.)
with native function calling. MCP tools are converted to OpenAI's function
calling format.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncGenerator, Optional

import httpx

from ..log_setup import get_logger
from .base import (
    BaseProvider,
    ToolCall,
    ToolDef,
    delta_event,
    end_event,
    error_event,
    tool_call_event,
)


class OpenAIProvider(BaseProvider):
    """Provider backed by an OpenAI-compatible API."""

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o",
        timeout: float = 60.0,
        name: str = "openai",
        thinking: Optional[bool] = None,
        reasoning_effort: Optional[str] = None,
        context_window: Optional[int] = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._name = name  # allows "deepseek", "ollama", etc.
        self._thinking = thinking  # None=default, True=enabled, False=disabled
        self._reasoning_effort = reasoning_effort  # None=default, "low"/"medium"/"high"
        self._context_window = context_window
        self._log = get_logger()
        self._client: Optional[httpx.AsyncClient] = None
        # Set to True after the first HTTP 400 "Unsupported parameter: thinking"
        # so we never waste a round-trip on this provider again.
        self._thinking_unsupported = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def supports_function_calling(self) -> bool:
        return True

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {
                "Content-Type": "application/json",
            }
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                timeout=httpx.Timeout(self._timeout),
            )
        return self._client

    async def chat(
        self,
        messages: list[dict],
        *,
        system: Optional[str] = None,
        tools: Optional[list[ToolDef]] = None,
        max_tokens: int = 512,
        temperature: float = 0.6,
        top_p: float = 0.95,
        **kwargs,
    ) -> AsyncGenerator[dict, None]:
        client = self._get_client()

        openai_messages = list(messages)
        if system:
            openai_messages.insert(0, {"role": "system", "content": system})

        openai_tools = self._openai_tool_format(tools) if tools else None

        body = {
            "model": self._model,
            "messages": openai_messages,
            "max_tokens": max(1, int(max_tokens)),
            "stream": True,
        }
        # Only set temperature/top_p if explicitly non-default
        if temperature != 0.6:
            body["temperature"] = max(0.0, float(temperature))
        if top_p != 0.95:
            body["top_p"] = float(top_p)
        if openai_tools:
            body["tools"] = openai_tools
            body["tool_choice"] = "auto"
        # DeepSeek / thinking mode: only emit the `thinking` field when the
        # provider explicitly opted in (self._thinking === True) AND we
        # haven't already learned this provider rejects it.
        if self._thinking is True and not self._thinking_unsupported:
            body["thinking"] = {"type": "enabled"}
        # Reasoning effort: passed as top-level field (OpenAI/DeepSeek compat)
        if self._reasoning_effort and not self._thinking_unsupported and self._reasoning_effort in ("low", "medium", "high", "xhigh", "max"):
            body["reasoning_effort"] = self._reasoning_effort

        # Track if we already retried without thinking in this call
        retried_without_thinking = False

        while True:
            try:
                async with client.stream("POST", "/chat/completions", json=body) as resp:
                    if resp.status_code == 400 and not retried_without_thinking:
                        error_body = (await resp.aread()).decode("utf-8", "ignore")[:500]
                        if "thinking" in error_body.lower() and "unsupported" in error_body.lower():
                            self._log.warning(
                                "Provider %s does not support thinking parameter, retrying without it",
                                self._name,
                            )
                            body.pop("thinking", None)
                            body.pop("reasoning_effort", None)
                            retried_without_thinking = True
                            self._thinking_unsupported = True
                            continue

                    if resp.status_code != 200:
                        error_body = (await resp.aread()).decode("utf-8", "ignore")[:500]
                        yield error_event(
                            f"OpenAI API HTTP {resp.status_code}: {error_body}"
                        )
                        return

                    tool_calls_acc: dict[int, dict] = {}

                    async for raw_line in resp.aiter_lines():
                        if not raw_line.startswith("data:"):
                            continue
                        payload = raw_line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            obj = json.loads(payload)
                        except json.JSONDecodeError:
                            continue

                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}

                        # Handle content (including reasoning_content from thinking models)
                        content = delta.get("content")
                        reasoning = delta.get("reasoning_content")
                        if reasoning:
                            # Yield as a "think" event so the renderer shows
                            # it with the same淡化 styling as MiniCPM5's
                            # imd block, not as regular reply text.
                            yield {"type": "think", "content": reasoning}
                        elif content:
                            yield delta_event(content)

                        # Handle tool calls
                        if "tool_calls" in delta:
                            for tc in delta["tool_calls"]:
                                idx = tc.get("index", 0)
                                if idx not in tool_calls_acc:
                                    tool_calls_acc[idx] = {
                                        "id": tc.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                                        "function": {"name": "", "arguments": ""},
                                    }
                                func = tc.get("function", {})
                                if func.get("name"):
                                    tool_calls_acc[idx]["function"]["name"] += func["name"]
                                if func.get("arguments"):
                                    tool_calls_acc[idx]["function"]["arguments"] += func["arguments"]

                        finish = choices[0].get("finish_reason")
                        if finish == "tool_calls":
                            for idx in sorted(tool_calls_acc.keys()):
                                tc_data = tool_calls_acc[idx]
                                try:
                                    args = json.loads(tc_data["function"]["arguments"])
                                except json.JSONDecodeError:
                                    args = {}
                                tc = ToolCall(
                                    id=tc_data["id"],
                                    server_name="default",
                                    name=tc_data["function"]["name"],
                                    arguments=args,
                                )
                                yield tool_call_event(tc)
                            tool_calls_acc.clear()
                        elif finish == "stop":
                            break
                        elif finish == "length":
                            self._log.warning(
                                "OpenAI finish_reason=length — output was truncated"
                            )
                            yield delta_event(
                                "\n\n[Response truncated — increase max_tokens for longer replies]"
                            )
                        elif finish:
                            self._log.debug("OpenAI finish_reason: %s", finish)

                # Successfully processed the stream, exit the retry loop
                break

            except httpx.TimeoutException:
                yield error_event("OpenAI API request timed out")
                return
            except Exception as exc:
                self._log.exception("OpenAIProvider chat error: %s", exc)
                yield error_event(str(exc))
                return

        yield end_event()

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
