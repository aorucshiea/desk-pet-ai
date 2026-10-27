"""OpenAI-compatible API provider.

Supports any OpenAI-compatible endpoint (OpenAI, DeepSeek, Together, etc.)
with native function calling. MCP tools are converted to OpenAI's function
calling format.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncGenerator, Optional
from urllib.parse import urlparse

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

# Hosts that mean "this endpoint runs on the user's machine". Local
# inference engines (LM Studio, llama.cpp, Ollama, vLLM, …) are slow to
# prefill a long prompt and must not be treated like a cloud API when we
# pick an output budget or an HTTP timeout.
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})

# Model-name markers for OpenAI-compatible text-only endpoints. This is a
# heuristic, not a whitelist: DeepSeek/Qwen/Gemini-compatible models are not
# listed here and therefore assumed to accept image inputs. A provider entry
# in providers.json can override it with "images" / "capabilities.images".
_NO_VISION_MODEL_MARKERS = ("gpt-3.5", "text-", "embedding", "davinci", "babbage")


def is_local_url(base_url: str) -> bool:
    """True when *base_url* points at a loopback endpoint."""
    try:
        host = (urlparse(str(base_url or "")).hostname or "").strip("[]").lower()
    except Exception:
        return False
    return host in _LOCAL_HOSTS


def model_supports_vision(model: str) -> bool:
    """Heuristic image-input support for an OpenAI-compatible model name."""
    name = str(model or "").lower()
    return not any(marker in name for marker in _NO_VISION_MODEL_MARKERS)


def _is_reasoning_content_error(body_text: str) -> bool:
    """True for the DeepSeek thinking-mode HTTP 400.

    Live error: ``The reasoning_content in the thinking mode must be passed
    back to the API.`` It means a previous assistant/tool_calls message lost
    its reasoning_content (text [MCP:] path, history replay, …). The only
    recovery from here is to stop sending thinking parameters and retry.
    """
    text = str(body_text or "").lower()
    return "reasoning_content" in text


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
        is_local: Optional[bool] = None,
        allow_tools: bool = True,
        supports_vision: Optional[bool] = None,
        supports_function_calling: Optional[bool] = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._name = name  # allows "deepseek", "ollama", etc.
        self._thinking = thinking  # None=default, True=enabled, False=disabled
        self._reasoning_effort = reasoning_effort  # None=default, "low"/"medium"/"high"
        self._context_window = context_window
        self._is_local = is_local_url(base_url) if is_local is None else bool(is_local)
        self._allow_tools = bool(allow_tools)
        # A bridge/provider may speak the OpenAI wire format but not actually
        # support native tool calls (e.g. a browser-driven web-chat bridge).
        # None keeps the historical True default.
        self._supports_function_calling = supports_function_calling
        # None = derive from the model name; explicit True/False from
        # providers.json ("images"/"capabilities.images") always wins.
        self._supports_vision = supports_vision
        self._log = get_logger()
        self._client: Optional[httpx.AsyncClient] = None
        # Set to True after the first HTTP 400 "Unsupported parameter: thinking"
        # so we never waste a round-trip on this provider again.
        self._thinking_unsupported = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model

    @property
    def supports_vision(self) -> bool:
        """Whether this endpoint accepts image_url content parts.

        Explicit configuration wins; otherwise the model-name heuristic
        applies. DeepSeek and other unlisted cloud models are treated as
        vision-capable (the vision encoder lives server-side).
        """
        if self._supports_vision is not None:
            return bool(self._supports_vision)
        return model_supports_vision(self._model)

    @property
    def is_local(self) -> bool:
        """True for loopback inference servers (LM Studio, llama.cpp, Ollama…)."""
        return self._is_local

    @property
    def allow_tools(self) -> bool:
        """Whether native tool calls are advertised to this provider.

        Local endpoints default to False: 30+ tool schemas are thousands of
        tokens of prompt, and a 2B model will happily call ``pet_mood_now``
        on a plain "你好" — each tool round-trip re-prefills the whole
        prompt. Users who run a capable local model can set
        ``"toolsEnabled": true`` in ~/.pet/providers.json.
        """
        return self._allow_tools

    @property
    def supports_function_calling(self) -> bool:
        if self._supports_function_calling is None:
            return True
        return bool(self._supports_function_calling)

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
                # read=timeout is per streamed chunk, so a slow local prefill
                # gets its full budget; connect stays short so a closed LM
                # Studio fails fast instead of waiting the whole 300 s.
                timeout=httpx.Timeout(
                    connect=5.0 if self._is_local else 10.0,
                    read=self._timeout,
                    write=30.0,
                    pool=5.0,
                ),
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
        thinking_override: Optional[bool] = None,
        reasoning_effort_override: Optional[str] = None,
        **kwargs,
    ) -> AsyncGenerator[dict, None]:
        """Stream a completion.

        ``thinking_override`` / ``reasoning_effort_override`` let the gateway
        apply a per-request budget (Laya) without mutating the provider's
        static providers.json defaults. ``None`` keeps the configured value.
        """
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
        # DeepSeek / thinking mode. A per-request override wins over
        # providers.json. When the effective mode is on, emit
        # {"type": "enabled"} unless this provider already rejected the
        # parameter. When it is off, omission is NOT enough: DeepSeek V4
        # defaults reasoning to on and its docs require an explicit
        # {"type": "disabled"} — a live canonical-prompt chat with
        # thinking=False still streamed a long reasoning block (~16 s).
        # Only send the disabled marker when providers.json explicitly
        # enabled thinking for a cloud endpoint. Unknown support (None),
        # already-off config (False), local OpenAI-compatible servers
        # (LM Studio / llama.cpp reject unknown fields) and providers that
        # already 400'd on the parameter keep the field absent entirely.
        thinking_mode = self._thinking if thinking_override is None else bool(thinking_override)
        if not self._thinking_unsupported:
            if thinking_mode is True:
                body["thinking"] = {"type": "enabled"}
            elif (
                thinking_mode is False
                and self._thinking is True
                and not self._is_local
            ):
                body["thinking"] = {"type": "disabled"}
        # Reasoning effort: passed as top-level field (OpenAI/DeepSeek compat).
        # The request override wins; when thinking is explicitly off we must
        # not smuggle the static effort back in (that would re-enable
        # reasoning on providers that key off reasoning_effort).
        effort = (
            self._reasoning_effort
            if reasoning_effort_override is None
            else reasoning_effort_override
        )
        if (
            effort
            and thinking_mode is not False
            and not self._thinking_unsupported
            and effort in ("low", "medium", "high", "xhigh", "max")
        ):
            body["reasoning_effort"] = effort

        # Track if we already retried without thinking in this call
        retried_without_thinking = False
        # Separate one-shot guard for the reasoning_content 400: a provider
        # must never ping-pong between the two downgrade paths.
        retried_without_reasoning = False
        started = time.monotonic()
        first_token_logged = False

        while True:
            try:
                async with client.stream("POST", "/chat/completions", json=body) as resp:
                    if resp.status_code == 400:
                        error_body = (await resp.aread()).decode("utf-8", "ignore")
                        lowered = error_body.lower()
                        if not retried_without_thinking and "thinking" in lowered and "unsupported" in lowered:
                            self._log.warning(
                                "Provider %s does not support thinking parameter, retrying without it",
                                self._name,
                            )
                            body.pop("thinking", None)
                            body.pop("reasoning_effort", None)
                            retried_without_thinking = True
                            self._thinking_unsupported = True
                            continue
                        if not retried_without_reasoning and _is_reasoning_content_error(error_body):
                            # DeepSeek thinking mode: the follow-up round lost
                            # reasoning_content. Degrade this provider to
                            # non-thinking and retry exactly once; if it still
                            # fails the caller gets the error.
                            self._log.warning(
                                "Provider %s rejected a thinking-mode request (%s); "
                                "disabling thinking for this provider and retrying once",
                                self._name,
                                error_body[:200].replace("\n", " "),
                            )
                            body.pop("thinking", None)
                            body.pop("reasoning_effort", None)
                            retried_without_reasoning = True
                            self._thinking_unsupported = True
                            continue

                    if resp.status_code != 200:
                        error_body = (await resp.aread()).decode("utf-8", "ignore")[:500]
                        yield error_event(
                            f"OpenAI API HTTP {resp.status_code}: {error_body}"
                        )
                        return

                    tool_calls_acc: dict[int, dict] = {}
                    # Reasoning is streamed separately from visible content.
                    # Accumulate it for THIS round so a tool-call follow-up
                    # can pass it back (DeepSeek thinking mode rejects the
                    # next request with HTTP 400 otherwise).
                    reasoning_acc = ""

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
                        if self._is_local and not first_token_logged and (reasoning or content):
                            first_token_logged = True
                            # The first token is the whole UX for a local model:
                            # prompt prefill dominates wall-clock time, and a
                            # silent wait looks like a hang. Keep this line
                            # greppable ("first token") for bug reports.
                            self._log.info(
                                "provider %s first token after %.1fs (max_tokens=%s)",
                                self._name,
                                time.monotonic() - started,
                                body.get("max_tokens"),
                            )
                        if reasoning:
                            # Keep it out of the visible delta stream, but
                            # remember the full text for the tool-call
                            # assistant message.
                            reasoning_acc += reasoning
                            # Yield as a "think" event so the renderer shows
                            # it with the same淡化 styling as DeskPet5's
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
                                    reasoning_content=reasoning_acc,
                                )
                                yield tool_call_event(tc)
                            tool_calls_acc.clear()
                            # Reasoning belongs to the round that just ended;
                            # never concatenate it into the next tool round.
                            reasoning_acc = ""
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
                self._log.warning(
                    "provider %s timed out after %.0fs (max_tokens=%s)",
                    self._name,
                    self._timeout,
                    body.get("max_tokens"),
                )
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
