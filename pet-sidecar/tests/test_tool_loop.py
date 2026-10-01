"""The pet must keep thinking AFTER a tool call — autonomy regression.

Live symptom (2026-10-01): the model output a tool call
(`[MCP:builtin/recall:{...}]`) and the turn simply ended. chat-history.json
showed the assistant's tool-call text as the last entry, with no tool
result and no follow-up — the pet read as having no autonomy at all.

Root cause: for a non-function-calling provider (llama-server local), the
tool result was appended as `role: "tool"`. llama-server does not process
that role, so the follow-up generation came back empty, `tool_occurred`
stayed false and the loop broke after exactly one iteration. The model
quite literally never saw the result of the tool it had just called.

This pins both halves: the result must arrive as a user turn, and the
provider must be asked to generate again.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from gateway import server as server_mod
from gateway.server import ChatRequest


class _StubProvider:
    """Non-function-calling provider (same shape as local llama-server)."""

    name = "local"
    supports_function_calling = False

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            yield {"type": "delta", "content": "等我翻翻旧记忆。\n"}
            yield {"type": "tool_call", "id": "tc1", "server_name": "builtin",
                   "name": "recall", "arguments": {"keyword": "用户"}}
            yield {"type": "end"}
        else:
            yield {"type": "delta", "content": "翻到了，你碰过我三次。"}
            yield {"type": "end"}


class _StubMCP:
    def __init__(self) -> None:
        self.called: list[tuple] = []

    async def call_tool(self, server_name, tool_name, arguments):
        self.called.append((server_name, tool_name, arguments))
        return {"summary": "【你记得的事】用户碰过你三次"}


class _StubBridge:
    def new_session(self) -> None: ...
    def post(self, *_a, **_k) -> None: ...


class _StubMood:
    emotion_index = 0.0

    def format_for_system_prompt(self) -> str:
        return ""

    def apply_emotion_tag(self, _tag) -> None: ...


class _StubRegistry:
    def __init__(self, provider) -> None:
        self._providers = {"local": provider}
        self._default = provider

    def get(self, name):
        return self._providers.get(name, self._default)


def test_tool_result_is_fed_back_and_the_model_continues(monkeypatch):
    provider = _StubProvider()
    mcp = _StubMCP()

    # Resolve straight to our stub — the routing logic is covered elsewhere.
    monkeypatch.setattr(server_mod, "_resolve_provider", lambda *a, **k: provider)
    # No plugin kernel / no event store in this scenario.
    monkeypatch.setattr(server_mod, "_kernel", lambda: None, raising=False)
    monkeypatch.setattr(server_mod, "get_event_store", lambda: None, raising=False)

    async def _no_tools(_mcp_manager, _req):
        return None

    monkeypatch.setattr(server_mod, "_gather_tools", _no_tools)

    req = ChatRequest(messages=[{"role": "user", "content": "你想想"}])

    async def _drain():
        gen = server_mod._stream_chat_provider(
            _StubRegistry(provider), mcp, _StubBridge(), req,
            server=None, server_state={}, mood_store=_StubMood(), lora_arr=None,
        )
        out = []
        async for chunk in gen:
            out.append(chunk)
        return out

    # asyncio.run keeps this plugin-free (this suite has no pytest-asyncio).
    asyncio.run(_drain())

    # 1. the tool really ran
    assert mcp.called and mcp.called[0][1] == "recall", "the tool was never executed"

    # 2. THE POINT: the model was asked to generate again — autonomy
    assert len(provider.calls) >= 2, (
        "the provider was called only once — the turn died right after the "
        "tool call. This is the exact 'tool call then stop' bug."
    )

    # 3. and the result reached it as a user turn (not role:'tool')
    follow_up = provider.calls[1]
    blob = json.dumps(follow_up.get("messages", []), ensure_ascii=False, default=str)
    assert "recall 结果" in blob, (
        "the tool result never made it into the follow-up request"
    )
    assert '"role": "tool"' not in blob, (
        "result was fed back as role:'tool' — llama-server ignores that, "
        "which is what killed the follow-up generation"
    )
