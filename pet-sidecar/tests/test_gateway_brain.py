"""Gateway brain-stream wiring: events emitted by ``_stream_chat_provider``.

Drives the real provider generator with a scripted provider and a test D
store (same harness shape as test_gateway_cortex.py) while subscribed to the
in-process brain bus. Also pins the failure-isolation contract: a publish
that raises must never break the chat stream.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from gateway import brain
from gateway import server as server_mod
from gateway.decision import cortex
from gateway.decision.cortex import DStateStore
from gateway.providers.openai import OpenAIProvider
from gateway.server import ChatRequest


class _RecordingOpenAI(OpenAIProvider):
    """Real OpenAIProvider surface, no HTTP: records kwargs + script."""

    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        super().__init__(
            base_url="https://api.example.com/v1",
            model="deepseek-chat",
            name="deepseek",
            thinking=True,
            reasoning_effort="max",
        )
        self.script = [list(turn) for turn in script]
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.script) - 1)
        for event in self.script[index]:
            yield dict(event)


class _StubMCP:
    async def call_tool(self, server_name, tool_name, arguments):
        return {"summary": "ok"}


class _StubBridge:
    def new_session(self) -> None: ...

    def post(self, *_a, **_k) -> None: ...


class _StubMood:
    emotion_index = 0.0

    def format_for_system_prompt(self) -> str:
        return ""

    def apply_emotion_tag(self, _tag) -> None: ...


class _StubEphemeral:
    memory_dir = None

    def __init__(self) -> None:
        self.thoughts: list[tuple[str, str]] = []

    def format_for_prompt(self) -> str:
        return ""

    def add_thought(self, text, source="dice", confidence=0.0) -> None:
        self.thoughts.append((source, text))


class _StubRegistry:
    def __init__(self, provider) -> None:
        self._providers = {provider.name: provider}
        self._default = provider

    def get(self, name):
        return self._providers.get(name, self._default)


def _decision(**overrides) -> dict:
    value = {
        "decision_id": "d_test",
        "thinking": True,
        "reasoning_effort": "low",
        "strength": "low",
        "reaction": "small",
        "max_tokens": 2048,
        "action": "speak",
        "action_label": "说",
        "source": "test",
        "confidence": 0.75,
        "reason": "test",
    }
    value.update(overrides)
    return value


def _dstate(summary: str) -> str:
    return (
        "<<<DSTATE>>>"
        + json.dumps({"summary": summary}, ensure_ascii=False)
        + "<<<DSTATEEND>>>"
    )


def _install(monkeypatch, provider, *, passive=None):
    monkeypatch.setattr(server_mod, "_resolve_provider", lambda *_a, **_k: provider)
    monkeypatch.setattr(server_mod, "_kernel", lambda: None)
    monkeypatch.setattr(server_mod, "get_event_store", lambda: None)
    monkeypatch.setattr(server_mod, "_extraction_hook", None)
    monkeypatch.setattr(server_mod, "_ephemeral_ref", _StubEphemeral())

    async def _no_tools(*_a, **_k):
        return None

    monkeypatch.setattr(server_mod, "_gather_tools", _no_tools)
    if passive is not None:
        monkeypatch.setattr(cortex, "passive_decision", passive)


def _use_store(monkeypatch, tmp_path) -> DStateStore:
    store = DStateStore(Path(tmp_path) / "dstate.json")
    store.summary = "上一轮总结：用户在调试网关"
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    return store


def _run(provider, req):
    async def _drive():
        queue = brain.subscribe()
        frames: list[dict] = []
        try:
            async for chunk in server_mod._stream_chat_provider(
                _StubRegistry(provider),
                _StubMCP(),
                _StubBridge(),
                req,
                server=None,
                server_state={},
                mood_store=_StubMood(),
                lora_arr=None,
            ):
                text = chunk.decode("utf-8")
                if text.startswith("data: "):
                    frames.append(json.loads(text[6:]))
            events: list[dict] = []
            while not queue.empty():
                events.append(queue.get_nowait())
        finally:
            brain.unsubscribe(queue)
        return frames, events

    return asyncio.run(_drive())


def _req(**kwargs) -> ChatRequest:
    defaults = {
        "messages": [{"role": "user", "content": "今天怎么样"}],
        "max_new_tokens": 4096,
        "thinking": False,
        "tools_enabled": False,
    }
    defaults.update(kwargs)
    return ChatRequest(**defaults)


_PROACTIVE = [{"role": "user", "content": "[系统提示：该说话了]"}]


@pytest.fixture(autouse=True)
def _clean_brain():
    brain.clear()
    yield
    brain.clear()


def _types(events: list[dict]) -> list[str]:
    return [event["type"] for event in events]


def test_round_emits_the_full_brain_sequence(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision()

    provider = _RecordingOpenAI(
        [
            [
                {
                    "type": "tool_call",
                    "id": "tc_slow",
                    "server_name": "default",
                    "name": "pet_enter_slow",
                    "arguments": {"strength": "high"},
                },
                {"type": "end"},
            ],
            [
                {"type": "delta", "content": "我在呢。"},
                {"type": "delta", "content": _dstate("新的总结：用户在场")},
                {"type": "end"},
            ],
        ]
    )
    _install(monkeypatch, provider, passive=_passive)

    frames, events = _run(provider, _req())

    assert _types(events) == [
        "round_start",
        # D runs before provider resolution (it picks the thinking budget).
        "summary",
        "d_decision",
        "provider",
        "prompt",
        "tool_call",
        "slow",
        "tool_result",
        "first_token",
        "dstate",
        "speak",
        "round_end",
    ]
    assert len({event["round"] for event in events}) == 1
    assert events[0]["round"].startswith("r_")

    round_start = events[0]
    assert round_start["title"] == "轮开始"
    assert round_start["meta"]["proactive"] is False
    assert round_start["meta"]["messages"] == 1
    assert round_start["meta"]["user_text"] == "今天怎么样"

    provider_event = events[3]
    assert provider_event["meta"]["name"] == "deepseek"
    assert provider_event["meta"]["model"] == "deepseek-chat"
    assert provider_event["meta"]["is_local"] is False

    assert events[1]["text"] == "上一轮总结：用户在调试网关"
    assert events[1]["meta"]["chars"] == len("上一轮总结：用户在调试网关")
    d_decision = events[2]
    assert "这一轮你的底层直觉" in d_decision["text"]
    assert d_decision["meta"]["decision_id"] == "d_test"
    assert d_decision["meta"]["action"] == "speak"

    tool_call = events[5]
    assert tool_call["title"] == "工具 · pet_enter_slow"
    assert json.loads(tool_call["text"]) == {"strength": "high"}
    assert tool_call["meta"]["tool"] == "pet_enter_slow"
    assert tool_call["meta"]["iteration"] == 1
    assert events[6]["meta"]["strength"] == "high"

    tool_result = events[7]
    assert tool_result["title"] == "结果 · pet_enter_slow"
    assert tool_result["meta"]["is_error"] is False
    assert tool_result["meta"]["elapsed_ms"] >= 0
    assert tool_result["meta"]["iteration"] == 1

    dstate = events[9]
    assert dstate["text"] == "新的总结：用户在场"
    assert dstate["meta"]["updated"] is True
    assert dstate["meta"]["chars"] == len("新的总结：用户在场")

    speak = events[10]
    assert speak["title"] == "说出来"
    assert "我在呢。" in speak["text"]
    assert speak["meta"]["chars"] >= 3

    round_end = events[11]
    assert round_end["meta"]["iterations"] == 2
    assert round_end["meta"]["tools"] == 1
    assert round_end["meta"]["provider"] == "deepseek"
    assert round_end["meta"]["elapsed_ms"] >= 0

    visible = "".join(
        frame.get("content", "")
        for frame in frames
        if frame.get("event") == "delta"
    )
    assert "我在呢。" in visible
    assert "DSTATE" not in visible
    assert store.summary == "新的总结：用户在场"


def test_quiet_life_turn_emits_inner_and_quiet_without_speech(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision(action="quiet", action_label="安静")

    provider = _RecordingOpenAI(
        [
            [
                {"type": "delta", "content": "[INNER:桌面很安静，我在发呆]"},
                {"type": "delta", "content": _dstate("困倦但平静")},
                {"type": "end"},
            ]
        ]
    )
    _install(monkeypatch, provider, passive=_passive)

    frames, events = _run(provider, _req(messages=_PROACTIVE))

    types = _types(events)
    assert "inner" in types
    assert "quiet" in types
    assert "speak" not in types
    assert "delta" not in [frame["event"] for frame in frames]

    inner = next(event for event in events if event["type"] == "inner")
    assert inner["title"] == "心声"
    assert inner["text"] == "桌面很安静，我在发呆"
    assert inner["meta"]["chars"] == len("桌面很安静，我在发呆")

    quiet = next(event for event in events if event["type"] == "quiet")
    assert quiet["title"] == "安静 · 心声藏起来"
    assert quiet["meta"]["reason"] == "quiet_decision"
    assert quiet["meta"]["hidden_chars"] >= len("桌面很安静，我在发呆")

    dstate = next(event for event in events if event["type"] == "dstate")
    assert dstate["meta"]["updated"] is True
    assert dstate["text"] == "困倦但平静"
    assert events[-1]["type"] == "round_end"


def test_tag_only_proactive_round_is_quiet_not_speak(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision(action="speak", action_label="说")

    # The model split its MEM block across tool rounds and this final round
    # contains only the closing marker.
    provider = _RecordingOpenAI(
        [[{"type": "delta", "content": "<<<MEMEND>>>"}, {"type": "end"}]]
    )
    _install(monkeypatch, provider, passive=_passive)

    frames, events = _run(provider, _req(messages=_PROACTIVE))

    types = _types(events)
    assert "speak" not in types
    assert "quiet" in types
    quiet = next(event for event in events if event["type"] == "quiet")
    assert quiet["meta"]["reason"] == "proactive_no_text"
    assert quiet["meta"]["hidden_chars"] == 0
    assert server_mod._visible_reply_text("".join(
        frame.get("content", "")
        for frame in frames
        if frame.get("event") == "delta"
    )) == ""


def test_brain_publish_exception_does_not_break_chat(monkeypatch):
    def _boom(_event):
        raise RuntimeError("brain bus is down")

    monkeypatch.setattr(brain, "publish", _boom)
    monkeypatch.setattr(server_mod, "_dstate_provider", None)

    provider = _RecordingOpenAI(
        [[{"type": "delta", "content": "还是能说话。"}, {"type": "end"}]]
    )
    _install(monkeypatch, provider)

    frames, events = _run(provider, _req())

    assert events == []
    visible = "".join(
        frame.get("content", "")
        for frame in frames
        if frame.get("event") == "delta"
    )
    assert visible == "还是能说话。"
    assert frames[0]["event"] == "start"
    assert frames[-1]["event"] == "end"
