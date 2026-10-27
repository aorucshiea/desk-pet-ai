"""End-to-end SSE contract for ``POST /api/chat`` (BACKLOG B-11).

The chat pipeline (start / delta / think / tool loop / MEM extraction /
next_chat / end) had no end-to-end coverage at the HTTP boundary: earlier
tests either drained the generator directly or exercised one helper in
isolation. These cases drive the real FastAPI app with a scripted fake
provider and parse the actual ``data: {json}`` frames a renderer would see.

No model, network or real clawd-on-desk server is involved: the provider is
monkeypatched, ClawdBridge.post is a no-op, and the desktop-world probe is
stubbed so a run cannot fall through to 127.0.0.1:23333-23337.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway import server as server_mod
from gateway.memory import impulse as impulse_mod


# ── Scripted provider ────────────────────────────────────────────────


class FakeProvider:
    """Same surface the gateway expects from a local, non-tool-calling engine."""

    name = "fake"
    supports_function_calling = False
    is_local = True

    def __init__(self, script: list[list[dict[str, Any]]], *, allow_tools: bool = True):
        # Each entry is one provider.chat() call (the tool loop calls it twice).
        self.script = [list(turn) for turn in script]
        self.allow_tools = allow_tools
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.script) - 1)
        for event in self.script[index]:
            yield dict(event)


def _delta(text: str) -> dict[str, Any]:
    return {"type": "delta", "content": text}


def _end() -> dict[str, Any]:
    return {"type": "end"}


# ── HTTP / SSE helpers ───────────────────────────────────────────────


def _gateway_token() -> str:
    """The conftest token injector does not cover httpx.Client.stream()."""
    try:
        return (Path.home() / ".pet" / "gateway-token").read_text("utf-8").strip()
    except OSError:
        return ""


def _sse_frames(response) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for line in response.iter_lines():
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        line = line.rstrip("\r")
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload:
            frames.append(json.loads(payload))
    return frames


def _stream_chat(client: TestClient, *, messages=None, **overrides):
    body: dict[str, Any] = {
        "messages": messages or [{"role": "user", "content": "你好呀，今天怎么样？"}],
        "stream": True,
    }
    body.update(overrides)
    with client.stream(
        "POST",
        "/api/chat",
        json=body,
        headers={"x-pet-token": _gateway_token()},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return _sse_frames(response)


@pytest.fixture
def make_chat_client(monkeypatch):
    """Build a real app per test with every outbound side channel stubbed."""

    monkeypatch.setenv("PET_THEME", "default")
    # Fallback extraction is explicitly enabled only by its own test.
    monkeypatch.setenv("PET_EXTRACT_FALLBACK", "0")

    # The bridge probes 23333-23337 on every state push; tests must not.
    monkeypatch.setattr(server_mod.ClawdBridge, "post", lambda *_a, **_k: None)

    async def _no_world_bodies(force: bool = False):
        return []

    monkeypatch.setattr(server_mod, "_world_bodies_for_decision", _no_world_bodies)

    # Do not read the user's real ~/.pet/mcp.json or spawn MCP subprocesses.
    async def _no_mcp_config(self):
        return {}

    monkeypatch.setattr(server_mod.MCPManager, "load_config", _no_mcp_config)

    def _make(provider: FakeProvider) -> TestClient:
        monkeypatch.setattr(server_mod, "_resolve_provider", lambda *_a, **_k: provider)
        app = server_mod.build_app(initial_model=None)
        return TestClient(app)

    return _make


# ── 1. Golden path ───────────────────────────────────────────────────


def test_golden_path_text_emotion_and_next_chat(make_chat_client):
    provider = FakeProvider([[
        _delta("你好呀，今天也要开心。\n"),
        _delta("[EMOTION:happy]"),
        _delta("[NEXT_CHAT:60]"),
        _end(),
    ]])

    with make_chat_client(provider) as client:
        frames = _stream_chat(client, tools_enabled=False)

    events = [frame["event"] for frame in frames]
    assert events[0] == "start", "the stream must open with a start frame"
    assert events[-1] == "end", "the stream must close with an end frame"

    text = "".join(frame["content"] for frame in frames if frame["event"] == "delta")
    assert "你好呀" in text
    assert "EMOTION" not in text, "the emotion control tag leaked into the visible reply"
    assert "NEXT_CHAT" not in text, "the next-chat control tag leaked into the visible reply"

    next_chat = [frame for frame in frames if frame["event"] == "next_chat"]
    assert len(next_chat) == 1
    assert next_chat[0]["seconds"] == 60
    assert next_chat[0]["explicit"] is True, "the model's own tag is explicit"

    first_delta = events.index("delta")
    assert first_delta < events.index("next_chat") < events.index("end")


# ── 2. Thinking ──────────────────────────────────────────────────────


def test_thinking_frames_are_forwarded_before_the_reply(make_chat_client):
    provider = FakeProvider([[
        {"type": "think", "content": "让我想想……"},
        _delta("想好了，今天适合散步。\n"),
        _end(),
    ]])

    with make_chat_client(provider) as client:
        frames = _stream_chat(client, tools_enabled=False)

    events = [frame["event"] for frame in frames]
    think = [frame for frame in frames if frame["event"] == "think"]
    assert think and "让我想想" in think[0]["content"]
    text = "".join(frame["content"] for frame in frames if frame["event"] == "delta")
    assert "适合散步" in text
    assert events.index("think") < events.index("delta")
    assert events[-1] == "end"


# ── 3. Death-note MEM extraction ─────────────────────────────────────


MEM_REPLY = (
    "好的，我记住了。\n<<<MEM>>>"
    '{"events":[{"title":"测试事件","content":"用户说了测试（平静）","weight":300}]}'
    "<<<MEMEND>>>"
)


def test_mem_block_is_persisted_once_by_the_death_note(make_chat_client):
    provider = FakeProvider([[_delta(MEM_REPLY), _end()]])

    with make_chat_client(provider) as client:
        frames = _stream_chat(client, tools_enabled=False)
        assert frames[-1]["event"] == "end"

        events_path = Path(os.environ["PET_MEMORY_DIR"]) / "events.json"
        assert events_path.is_file(), "the death-note hook never wrote events.json"
        stored = json.loads(events_path.read_text(encoding="utf-8"))["events"]
        matches = [event for event in stored if event["title"] == "测试事件"]
        assert len(matches) == 1
        assert matches[0]["content"] == "用户说了测试（平静）"

        # The renderer POSTs the same reply after the stream; that replay must
        # be an idempotent no-op (the gateway already took its death note).
        replay = client.post("/api/events/extract", json={"response_text": MEM_REPLY})
        assert replay.status_code == 200
        assert replay.json()["duplicate"] is True
        assert replay.json()["events_added"] == []

        stored_again = json.loads(events_path.read_text(encoding="utf-8"))["events"]
        assert len([e for e in stored_again if e["title"] == "测试事件"]) == 1


# ── 4. Missing MEM block → background fallback ───────────────────────


def test_missing_mem_block_schedules_fallback_without_blocking_sse(
    make_chat_client, monkeypatch
):
    user_text = "今天发生了一件很有意思的事情"
    reply_text = "我听见了，慢慢说给我听。"
    state: dict[str, Any] = {"gate": None, "loop": None}
    calls: list[dict[str, Any]] = []
    started = threading.Event()
    finished = threading.Event()

    async def fake_run_fallback(
        provider_arg, user, reply, apply_extraction, *, timeout=90
    ):
        calls.append({
            "provider": provider_arg,
            "user": user,
            "reply": reply,
            "apply": apply_extraction,
            "timeout": timeout,
        })
        gate = asyncio.Event()
        state["gate"] = gate
        state["loop"] = asyncio.get_running_loop()
        started.set()
        try:
            # If SSE waited for this task, the cap makes the test finish with
            # ``finished`` already set — the assertion below then fails.
            await asyncio.wait_for(gate.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        finally:
            finished.set()
        return {"attempted": True}

    monkeypatch.setattr(server_mod._extract_fallback, "run_fallback", fake_run_fallback)
    monkeypatch.setenv("PET_EXTRACT_FALLBACK", "1")

    provider = FakeProvider([[_delta(reply_text + "\n"), _end()]], allow_tools=False)

    with make_chat_client(provider) as client:
        frames = _stream_chat(
            client,
            messages=[{"role": "user", "content": user_text}],
            tools_enabled=False,
        )
        assert frames[-1]["event"] == "end"
        assert started.wait(1.0), "run_fallback was never scheduled"
        try:
            assert calls
            assert calls[0]["provider"] is provider
            assert calls[0]["user"] == user_text
            assert reply_text in calls[0]["reply"]
            assert callable(calls[0]["apply"])
            assert calls[0]["timeout"] == 90
            assert not finished.is_set(), "the SSE response waited for extraction"
        finally:
            state["loop"].call_soon_threadsafe(state["gate"].set)
        assert finished.wait(1.0)


# ── 5. Missing [NEXT_CHAT] → impulse fallback ────────────────────────


def test_missing_next_chat_uses_impulse_within_bounds(make_chat_client):
    provider = FakeProvider([[
        _delta("今天没什么特别的事。\n"),
        _delta("[EMOTION:calm]"),
        _end(),
    ]])

    with make_chat_client(provider) as client:
        frames = _stream_chat(client, tools_enabled=False)

    next_chat = [frame for frame in frames if frame["event"] == "next_chat"]
    assert len(next_chat) == 1, "the model forgot [NEXT_CHAT]; impulse must step in"
    seconds = next_chat[0]["seconds"]
    assert impulse_mod.MIN_GAP_SECONDS <= seconds <= impulse_mod.MAX_GAP_SECONDS
    assert next_chat[0]["explicit"] is False, "the subconscious default is not explicit"
    assert frames[-1]["event"] == "end"


# ── 6. Provider error ────────────────────────────────────────────────


def test_provider_error_becomes_an_error_frame_not_http_500(make_chat_client):
    provider = FakeProvider([[
        _delta("说到一半……"),
        {"type": "error", "message": "provider exploded"},
    ]])

    with make_chat_client(provider) as client:
        frames = _stream_chat(client, tools_enabled=False)

    assert frames
    assert frames[0]["event"] == "start"
    assert frames[-1]["event"] == "error"
    assert frames[-1]["message"] == "provider exploded"
    assert "end" not in [frame["event"] for frame in frames]


# ── 7. Core tool loop with allow_tools=False ─────────────────────────


def test_core_memory_tool_call_keeps_the_stream_alive(make_chat_client):
    provider = FakeProvider(
        [
            [
                {
                    "type": "tool_call",
                    "id": "tc_memory_1",
                    "server_name": "builtin",
                    "name": "memory",
                    "arguments": {"action": "list"},
                },
                _end(),
            ],
            [_delta("记忆文件夹现在是空的。\n"), _end()],
        ],
        allow_tools=False,
    )

    with make_chat_client(provider) as client:
        frames = _stream_chat(client, tools_enabled=True)

    events = [frame["event"] for frame in frames]
    assert events[0] == "start"
    assert events[-1] == "end"
    assert "error" not in events

    text = "".join(frame["content"] for frame in frames if frame["event"] == "delta")
    assert "记忆文件夹现在是空的" in text
    assert "_执行 memory" in text, "the core tool never ran"

    assert len(provider.calls) == 2, "the tool result did not trigger a follow-up generation"
    follow_up = json.dumps(provider.calls[1]["messages"], ensure_ascii=False, default=str)
    assert "[memory 结果]" in follow_up
    assert '"role": "tool"' not in follow_up
