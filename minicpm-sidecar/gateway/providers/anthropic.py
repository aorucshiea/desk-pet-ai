"""Anthropic API provider for Claude models.

Supports native tool use with MCP tools converted to Anthropic's tool format.
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


ANTHROPIC_API = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(BaseProvider):
    """Provider backed by Anthropic's API (Claude models)."""

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = ANTHROPIC_API,
        model: str = "claude-sonnet-4-20250514",
        timeout: float = 60.0,
        name: str = "anthropic",
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._name = name
        self._log = get_logger()
        self._client: Optional[httpx.AsyncClient] = None

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
                "x-api-key": self._api_key,
                "anthropic-version": ANTHROPIC_VERSION,
            }
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

        system_prompt = system or ""
        anthropic_messages = []
        for m in messages:
            role = m.get("role", "")
            if role == "system":
                system_prompt = system_prompt or m.get("content", "")
                continue
            if role == "tool":
                anthropic_messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.get("tool_call_id", ""),
                            "content": m.get("content", ""),
                        }
                    ],
                })
            else:
                anthropic_messages.append(m)

        anthropic_tools = self._anthropic_tool_format(tools) if tools else None

        body: dict[str, Any] = {
            "model": self._model,
            "messages": anthropic_messages,
            "max_tokens": max(1, int(max_tokens)),
            "temperature": max(0.0, float(temperature)),
            "top_p": float(top_p),
            "stream": True,
        }
        if system_prompt:
            body["system"] = system_prompt
        if anthropic_tools:
            body["tools"] = anthropic_tools

        # Track tool use blocks detected during the stream
        pending_tool_calls: list[ToolCall] = []

        try:
            async with client.stream("POST", "/messages", json=body) as resp:
                if resp.status_code != 200:
                    error_body = (await resp.aread()).decode("utf-8", "ignore")[:500]
                    yield error_event(
                        f"Anthropic API HTTP {resp.status_code}: {error_body}"
                    )
                    return

                in_tool_block = False
                current_tool_call: Optional[dict] = None

                async for raw_line in resp.aiter_lines():
                    if not raw_line.startswith("data: "):
                        continue
                    payload = raw_line[6:].strip()
                    if not payload:
                        continue
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    ev_type = obj.get("type", "")

                    if ev_type == "content_block_start":
                        block = obj.get("content_block", {})
                        if block.get("type") == "tool_use":
                            in_tool_block = True
                            current_tool_call = {
                                "id": block.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
                                "name": block.get("name", ""),
                                "input": block.get("input", {}),
                                "input_acc": "",
                            }

                    elif ev_type == "content_block_delta":
                        delta = obj.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield delta_event(delta.get("text", ""))
                        elif delta.get("type") == "input_json_delta" and current_tool_call is not None:
                            current_tool_call["input_acc"] += delta.get("partial_json", "")

                    elif ev_type == "content_block_stop":
                        if in_tool_block and current_tool_call is not None:
                            # Parse accumulated JSON input
                            input_data = current_tool_call["input"]
                            if current_tool_call.get("input_acc"):
                                try:
                                    input_data = json.loads(current_tool_call["input_acc"])
                                except json.JSONDecodeError:
                                    pass
                            raw_name = current_tool_call["name"]
                            pending_tool_calls.append(ToolCall(
                                id=current_tool_call["id"],
                                server_name="default",
                                name=raw_name,
                                arguments=input_data,
                            ))
                            in_tool_block = False
                            current_tool_call = None

                    elif ev_type == "message_stop":
                        break

                    elif ev_type == "error":
                        error_data = obj.get("error", {})
                        yield error_event(
                            error_data.get("message", "Anthropic API error")
                        )
                        return

        except httpx.TimeoutException:
            yield error_event("Anthropic API request timed out")
            return
        except Exception as exc:
            self._log.exception("AnthropicProvider chat error: %s", exc)
            yield error_event(str(exc))
            return

        # Yield tool calls collected during the stream
        for tc in pending_tool_calls:
            yield tool_call_event(tc)

        yield end_event()

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
