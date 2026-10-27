"""Gateway wiring for the D core (cortex.py) and the hidden DSTATE block.

The gateway asks D once per non-silent turn (before provider resolution),
applies its thinking/reaction budget, injects the previous D summary plus
the decision prompt, and parses the main LLM's end-of-round DSTATE block
back into the per-theme store. These tests drive the real
``_stream_chat_provider`` with a scripted provider and a test D store.

No D store (direct unit tests, legacy callers) must keep the old
behaviour: no injection, no D call, no budget override.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from gateway import brain as brain_mod
from gateway import server as server_mod
from gateway import telemetry
from gateway.decision import cortex
from gateway.decision.cortex import DStateStore
from gateway.providers.openai import OpenAIProvider
from gateway.server import ChatRequest


# ── Stubs ───────────────────────────────────────────────────────────


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
    emotion_index = 0.1

    def format_for_system_prompt(self) -> str:
        return ""

    def apply_emotion_tag(self, _tag) -> None: ...


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
        "reason": "test|thinking=low,reaction=small,action=speak",
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

    async def _no_tools(*_a, **_k):
        return None

    monkeypatch.setattr(server_mod, "_gather_tools", _no_tools)
    if passive is not None:
        monkeypatch.setattr(cortex, "passive_decision", passive)


def _run(provider, req) -> list[dict]:
    async def _drain():
        frames: list[dict] = []
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
        return frames

    return asyncio.run(_drain())


def _req(**kwargs) -> ChatRequest:
    defaults = {
        "messages": [{"role": "user", "content": "今天怎么样"}],
        "max_new_tokens": 4096,
        "thinking": False,
        "tools_enabled": False,
    }
    defaults.update(kwargs)
    return ChatRequest(**defaults)


def _use_store(monkeypatch, tmp_path) -> DStateStore:
    store = DStateStore(Path(tmp_path) / "dstate.json")
    store.summary = "上一轮总结：用户在调试网关"
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    return store


def _visible(frames: list[dict]) -> str:
    return "".join(
        frame.get("content", "")
        for frame in frames
        if frame.get("event") == "delta"
    )


# ── Visible-text scrubbing: a MEM tag is never speech ──────────────


def test_visible_reply_text_strips_stray_mem_tags():
    assert server_mod._visible_reply_text("<<<MEMEND>>>") == ""
    assert server_mod._visible_reply_text("正文<<<MEMEND>>>") == "正文"
    assert server_mod._visible_reply_text("正文<<<MEM>>>") == "正文"
    assert server_mod._visible_reply_text("<<<memend>>>") == ""
    # Complete blocks are still stripped, not just their markers.
    assert (
        server_mod._visible_reply_text(
            '前文<<<MEM>>>{"events": []}<<<MEMEND>>>后文'
        )
        == "前文后文"
    )


# ── D applied to the provider call + prompt injection ──────────────


def test_set_dstate_provider_installs_and_resets(tmp_path):
    store = DStateStore(Path(tmp_path) / "dstate.json")
    try:
        server_mod.set_dstate_provider(lambda: store)
        assert server_mod._get_dstate_store() is store
        server_mod.set_dstate_provider(None)
        assert server_mod._get_dstate_store() is None
    finally:
        server_mod.set_dstate_provider(None)


def test_provider_failure_returns_none():
    def _boom():
        raise RuntimeError("no theme yet")

    try:
        server_mod.set_dstate_provider(_boom)
        assert server_mod._get_dstate_store() is None
    finally:
        server_mod.set_dstate_provider(None)


def test_cortex_can_be_disabled_by_env(monkeypatch, tmp_path):
    assert server_mod._cortex_enabled() is True
    monkeypatch.setenv("PET_CORTEX_ENABLED", "0")
    assert server_mod._cortex_enabled() is False

    _use_store(monkeypatch, tmp_path)
    calls: list[Any] = []

    async def _passive(*_a, **_kw):
        calls.append(1)
        return _decision()

    provider = _RecordingOpenAI([[{"type": "delta", "content": "好"}], [{"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    _run(provider, _req())

    assert calls == []
    assert "【你的底层直觉状态" not in (provider.calls[0]["system"] or "")


def test_d_decision_applies_budget_and_injects_previous_summary(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)
    states: list[dict] = []

    async def _passive(_store, state=None, **kwargs):
        states.append(dict(state or {}))
        assert kwargs["timeout"] == 1.0
        return _decision()

    provider = _RecordingOpenAI([[{"type": "delta", "content": "好"}], [{"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    req = _req(thinking=False)
    frames = _run(provider, req)

    assert any(frame.get("event") == "end" for frame in frames)
    assert states and states[0]["user_text"] == "今天怎么样"
    assert states[0]["is_proactive"] is False

    call = provider.calls[0]
    assert call["thinking_override"] is True
    assert call["reasoning_effort_override"] == "low"
    assert call["max_tokens"] == 2048
    assert req.thinking is True
    assert req.reasoning_effort == "low"
    assert req.max_new_tokens == 2048

    system = call["system"]
    assert "【你的底层直觉状态" in system
    assert "上一轮总结：用户在调试网关" in system
    assert "【这一轮你的底层直觉】" in system
    assert "默认动作：说" in system
    assert "max_tokens=2048" in system


def test_quiet_action_is_forced_to_speak_on_a_real_user_turn(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision(action="quiet", action_label="安静")

    provider = _RecordingOpenAI([[{"type": "delta", "content": "好"}], [{"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    _run(provider, _req())

    system = provider.calls[0]["system"]
    assert "默认动作：说" in system
    assert "默认动作：安静" not in system


def test_proactive_turn_keeps_quiet_default(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision(action="quiet", action_label="安静")

    provider = _RecordingOpenAI([[{"type": "delta", "content": "好"}], [{"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    _run(provider, _req(messages=[{"role": "user", "content": "[系统提示：该说话了]"}]))
    assert "默认动作：安静" in provider.calls[0]["system"]


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
    brain_mod.clear()

    frames = _run(
        provider,
        _req(messages=[{"role": "user", "content": "[系统提示：该说话了]"}]),
    )

    # The quiet/speak classification is a brain-stream event, not a chat
    # delta: the surface must stay silent either way.
    brain_events = brain_mod.snapshot()
    assert [e for e in brain_events if e.get("type") == "speak"] == []
    quiet = [e for e in brain_events if e.get("type") == "quiet"]
    assert quiet and quiet[-1]["meta"].get("reason") == "proactive_no_text"
    assert server_mod._visible_reply_text(_visible(frames)) == ""


# ── DSTATE end-of-round contract ────────────────────────────────────


def test_dstate_block_is_hidden_and_updates_the_store(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision()

    block = _dstate("新的总结：用户在写网关测试")
    provider = _RecordingOpenAI(
        [
            [
                {"type": "delta", "content": "我在呢。"},
                {"type": "delta", "content": "<<<DSTATE>>>{\"summary\":\"新的"},
                {"type": "delta", "content": "总结：用户在写网关测试\"}<<<DSTATEEND>>>"},
                {"type": "end"},
            ]
        ]
    )
    _install(monkeypatch, provider, passive=_passive)

    frames = _run(provider, _req())

    visible = _visible(frames)
    assert "我在呢。" in visible
    assert "DSTATE" not in visible
    assert "新的总结" not in visible
    assert store.summary == "新的总结：用户在写网关测试"
    assert store.path.exists()
    assert "新的总结" in store.path.read_text(encoding="utf-8")
    assert block  # sanity: the fixture string is what the provider streamed


def test_next_round_injects_the_new_summary(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision()

    provider = _RecordingOpenAI(
        [
            [
                {"type": "delta", "content": _dstate("第二轮的新总结")},
                {"type": "end"},
            ]
        ]
    )
    _install(monkeypatch, provider, passive=_passive)

    _run(provider, _req())
    assert store.summary == "第二轮的新总结"

    # Round 2: the newly persisted summary is what the main LLM sees.
    _run(provider, _req())
    assert "第二轮的新总结" in provider.calls[1]["system"]
    assert "上一轮总结：用户在调试网关" not in provider.calls[1]["system"]


def test_unterminated_dstate_is_stripped_even_without_a_store(monkeypatch):
    provider = _RecordingOpenAI(
        [
            [
                {"type": "delta", "content": "答案"},
                {"type": "delta", "content": '<<<DSTATE>>>{"summary":"没写完'},
                {"type": "end"},
            ]
        ]
    )
    _install(monkeypatch, provider)
    monkeypatch.setattr(server_mod, "_dstate_provider", None)

    frames = _run(provider, _req())

    visible = _visible(frames)
    assert visible == "答案"
    assert "DSTATE" not in visible


# ── No D store = legacy behaviour ──────────────────────────────────


def test_no_store_means_no_injection_and_legacy_budget(monkeypatch):
    calls: list[Any] = []

    async def _passive(*_a, **_kw):
        calls.append(1)
        return _decision()

    provider = _RecordingOpenAI([[{"type": "delta", "content": "好"}], [{"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)
    monkeypatch.setattr(server_mod, "_dstate_provider", None)

    # Code fence + 报错: the legacy meta seat keeps thinking=True here.
    req = _req(
        messages=[
            {
                "role": "user",
                "content": "```python\nraise KeyError(1)\n```\n为什么报错？",
            }
        ],
        thinking=True,
        reasoning_effort="high",
    )
    _run(provider, req)

    assert calls == [], "D must not be consulted without a store"
    system = provider.calls[0]["system"] or ""
    assert "【你的底层直觉状态" not in system
    # Legacy cloud floor and the requested thinking effort survive.
    assert provider.calls[0]["max_tokens"] == 8192
    assert provider.calls[0]["thinking_override"] is True
    assert provider.calls[0]["reasoning_effort_override"] == "high"


def test_d_exception_falls_back_to_safe_budget(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        raise RuntimeError("laya down")

    provider = _RecordingOpenAI([[{"type": "delta", "content": "好"}], [{"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    _run(provider, _req())

    call = provider.calls[0]
    assert call["thinking_override"] is False
    assert call["reasoning_effort_override"] is None
    assert call["max_tokens"] == cortex.REACTION_MAX_TOKENS["medium"]
    assert "【这一轮你的底层直觉】" in call["system"]


# ── pet_enter_slow: same-round slow-intuition upgrade ──────────────


def test_pet_enter_slow_upgrades_the_rest_of_the_round(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision(
            thinking=False,
            reasoning_effort=None,
            strength="no_thinking",
        )

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
            [{"type": "delta", "content": "想清楚了。\n"}, {"type": "end"}],
        ]
    )
    _install(monkeypatch, provider, passive=_passive)

    before = telemetry.snapshot()["counters"].get("decision.slow.enter", 0)
    frames = _run(provider, _req())
    after = telemetry.snapshot()["counters"].get("decision.slow.enter", 0)

    assert after == before + 1
    assert len(provider.calls) == 2
    # D started the round without thinking; the tool upgraded the REST of it.
    assert provider.calls[0]["thinking_override"] is False
    assert provider.calls[1]["thinking_override"] is True
    assert provider.calls[1]["reasoning_effort_override"] == "high"

    follow_up = json.dumps(provider.calls[1]["messages"], ensure_ascii=False, default=str)
    assert "slow intuition: high" in follow_up
    assert any(frame.get("event") == "delta" for frame in frames)


def test_slow_and_active_decision_tools_are_core():
    assert "pet_enter_slow" in server_mod.CORE_TOOL_NAMES
    assert "pet_decision" in server_mod.CORE_TOOL_NAMES


# ── pet_decision: active D tool (never mutates the store) ───────────


def test_pet_decision_tool_returns_json_and_summary(monkeypatch, tmp_path):
    store = DStateStore(Path(tmp_path) / "dstate.json")
    store.last_decision = {"action": "look"}
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)

    seen: list[tuple] = []

    async def _active_active(_store, state=None, **kwargs):
        seen.append((state, kwargs))
        return _decision(strength="low", reaction="small", action="look")

    monkeypatch.setattr(cortex, "active_decision", _active_active)
    token = server_mod._active_user_text_ref.set("看这边")

    try:
        before = telemetry.snapshot()["counters"].get("decision.cortex.active", 0)
        result = asyncio.run(
            server_mod._decision_tool(
                {
                    "context": "桌面上开着编辑器",
                    "action_options": [
                        {"id": "observe", "label": "看"},
                        {"id": "chat", "label": "聊"},
                    ],
                }
            )
        )
        after = telemetry.snapshot()["counters"].get("decision.cortex.active", 0)
    finally:
        server_mod._active_user_text_ref.reset(token)

    assert result["is_error"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["decision_id"] == "d_test"
    assert result["summary"] == "主动 D → low/small/look"
    assert after == before + 1

    state, kwargs = seen[0]
    assert state == {
        "user_text": "看这边",
        "is_proactive": False,
        "last_action": "look",
    }
    assert kwargs["context"] == "桌面上开着编辑器"
    assert kwargs["action_options"][0]["id"] == "observe"
    assert kwargs["timeout"] == server_mod.CORTEX_DECISION_TIMEOUT == 1.0
    # active_decision does not save: the on-disk store was never created.
    assert store.path.exists() is False


def test_pet_decision_tool_missing_store_is_soft(monkeypatch):
    monkeypatch.setattr(server_mod, "_dstate_provider", None)

    result = asyncio.run(server_mod._decision_tool({}))

    assert result["is_error"] is True
    assert "D 还不可用" in result["content"][0]["text"]


# ── Quiet proactive rounds vs. the visible fallback ────────────────


def _tool_then_quiet_provider(quiet_tail: str) -> _RecordingOpenAI:
    """Round 1 calls a tool; the forced tools=None catch-up says ``quiet_tail``."""
    return _RecordingOpenAI(
        [
            [
                {
                    "type": "tool_call",
                    "id": "tc_recall",
                    "server_name": "builtin",
                    "name": "recall",
                    "arguments": {"keyword": "用户"},
                },
                {"type": "end"},
            ],
            [{"type": "end"}],
            [{"type": "delta", "content": quiet_tail}, {"type": "end"}],
        ]
    )


def test_proactive_round_may_end_silently_and_keeps_dstate(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision(action="quiet", action_label="安静")

    provider = _tool_then_quiet_provider(
        _dstate("安静地继续活着") + "[NEXT_CHAT:30]"
    )
    _install(monkeypatch, provider, passive=_passive)

    frames = _run(
        provider,
        _req(messages=[{"role": "user", "content": "[系统提示：该说话了]"}]),
    )

    text = _visible(frames)
    assert server_mod.TOOL_LOOP_FALLBACK_TEXT not in text
    # The quiet life turn also hides the "_执行 recall..._" tool aside.
    assert "delta" not in [frame["event"] for frame in frames]
    next_chat = [f for f in frames if f.get("event") == "next_chat"]
    assert len(next_chat) == 1
    assert next_chat[0]["seconds"] == 30
    assert next_chat[0]["explicit"] is True
    assert store.summary == "安静地继续活着"
    assert store.path.exists()
    assert frames[-1]["event"] == "end"


def test_real_user_turn_still_gets_the_visible_fallback(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision()

    provider = _tool_then_quiet_provider("")
    _install(monkeypatch, provider, passive=_passive)

    frames = _run(provider, _req())

    text = _visible(frames)
    assert server_mod.TOOL_LOOP_FALLBACK_TEXT in text
    assert frames[-1]["event"] == "end"


# ── Quiet life round: silent surface, inner voice kept ─────────────


class _FakeEphemeral:
    """Records add_thought calls instead of writing a theme memory dir."""

    memory_dir = None

    def __init__(self) -> None:
        self.thoughts: list[tuple[str, str]] = []

    def format_for_prompt(self) -> str:
        return ""

    def add_thought(self, text, source="dice", confidence=0.0) -> None:
        self.thoughts.append((source, text))


_PROACTIVE = [{"role": "user", "content": "[系统提示：该说话了]"}]


def test_decision_to_prompt_quiet_adds_the_inner_voice_instruction():
    quiet = cortex.decision_to_prompt(_decision(action="quiet", action_label="安静"))
    assert "这一轮是心声模式" in quiet
    assert "[INNER:...]" in quiet
    assert "不要空着" in quiet
    assert "用户直接说话必须回应" in quiet
    assert "默认动作：安静" in quiet

    for action, label in (("speak", "说"), ("walk", "走"), ("look", "看"), ("other", "其他")):
        other = cortex.decision_to_prompt(_decision(action=action, action_label=label))
        assert "这一轮是心声模式" not in other
        assert "[INNER:...]" not in other
        assert "用户直接说话必须回应" in other
        assert f"默认动作：{label}" in other


def test_quiet_life_turn_hides_visible_text_but_stores_the_thought(
    monkeypatch, tmp_path
):
    store = _use_store(monkeypatch, tmp_path)
    ephemeral = _FakeEphemeral()
    monkeypatch.setattr(server_mod, "_ephemeral_ref", ephemeral)

    async def _passive(_store, state=None, **_kw):
        return _decision(action="quiet", action_label="安静")

    provider = _RecordingOpenAI(
        [
            [
                {"type": "delta", "content": "窗外的光有点晃眼。"},
                {"type": "delta", "content": _dstate("安静地继续活着")},
                {"type": "end"},
            ]
        ]
    )
    _install(monkeypatch, provider, passive=_passive)

    before = telemetry.snapshot()["counters"].get("decision.quiet.life", 0)
    frames = _run(provider, _req(messages=_PROACTIVE))
    after = telemetry.snapshot()["counters"].get("decision.quiet.life", 0)

    assert "这一轮是心声模式" in provider.calls[0]["system"]
    assert "delta" not in [frame["event"] for frame in frames]
    assert frames[0]["event"] == "start"
    assert frames[-1]["event"] == "end"
    assert _visible(frames) == ""
    assert ("quiet_life", "（心声）窗外的光有点晃眼。") in ephemeral.thoughts
    assert store.summary == "安静地继续活着"
    assert store.path.exists()
    assert after == before + 1


def test_quiet_life_turn_keeps_the_inner_event_without_double_storing(
    monkeypatch, tmp_path
):
    store = _use_store(monkeypatch, tmp_path)
    ephemeral = _FakeEphemeral()
    monkeypatch.setattr(server_mod, "_ephemeral_ref", ephemeral)

    async def _passive(_store, state=None, **_kw):
        return _decision(action="quiet", action_label="安静")

    provider = _RecordingOpenAI(
        [
            [
                {"type": "delta", "content": "[INNER:桌面很安静，我有点困]"},
                {"type": "delta", "content": _dstate("困倦但平静")},
                {"type": "end"},
            ]
        ]
    )
    _install(monkeypatch, provider, passive=_passive)

    frames = _run(provider, _req(messages=_PROACTIVE))

    assert "delta" not in [frame["event"] for frame in frames]
    assert "inner" in [frame["event"] for frame in frames]
    assert ephemeral.thoughts == [
        ("inner", "（心声）桌面很安静，我有点困")
    ]
    assert store.summary == "困倦但平静"
    assert frames[-1]["event"] == "end"


def test_user_turn_quiet_decision_is_not_suppressed(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)
    ephemeral = _FakeEphemeral()
    monkeypatch.setattr(server_mod, "_ephemeral_ref", ephemeral)

    async def _passive(_store, state=None, **_kw):
        return _decision(action="quiet", action_label="安静")

    provider = _RecordingOpenAI(
        [
            [
                {"type": "delta", "content": "在的，我听见了。"},
                {"type": "delta", "content": _dstate("用户在和我说话")},
                {"type": "end"},
            ]
        ]
    )
    _install(monkeypatch, provider, passive=_passive)

    frames = _run(provider, _req())

    # Force-speak rewrote the decision before the quiet flag was computed.
    assert "默认动作：说" in provider.calls[0]["system"]
    assert "这一轮是心声模式" not in provider.calls[0]["system"]
    assert "在的，我听见了。" in _visible(frames)
    assert ephemeral.thoughts == []
    assert store.summary == "用户在和我说话"
    assert frames[-1]["event"] == "end"


def test_quiet_life_turn_suppresses_the_forced_catchup(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)
    ephemeral = _FakeEphemeral()
    monkeypatch.setattr(server_mod, "_ephemeral_ref", ephemeral)

    async def _passive(_store, state=None, **_kw):
        return _decision(action="quiet", action_label="安静")

    provider = _tool_then_quiet_provider("（有点困，先趴一会儿）[NEXT_CHAT:20]")
    _install(monkeypatch, provider, passive=_passive)

    frames = _run(provider, _req(messages=_PROACTIVE))

    assert "delta" not in [frame["event"] for frame in frames]
    assert _visible(frames) == ""
    assert ("quiet_life", "（心声）（有点困，先趴一会儿）") in ephemeral.thoughts
    assert frames[-1]["event"] == "end"
