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

import httpx
import pytest

from gateway import server as server_mod
from gateway.providers.base import ToolDef
from gateway.providers.openai import OpenAIProvider, _is_reasoning_content_error
from gateway.server import ChatRequest
from gateway.telemetry import Telemetry


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

    # A real final body means no forced catch-up call: exactly one tool
    # round + one answer round.
    assert len(provider.calls) == 2, "the loop re-asked after a real final answer"


# ── Silent tool streak: the ONLY hard stop ────────────────────────────
# The loop has no total iteration/duration cap. 工具→说话→工具→说话 may run
# indefinitely; only consecutive tool calls with no visible reply for the
# whole budget get cut and force a final answer.


def test_silent_tool_timed_out_boundary():
    assert server_mod.SILENT_TOOL_TIMEOUT_SECONDS == 600
    assert not server_mod.silent_tool_timed_out(100.0, 699.999, 600)
    assert server_mod.silent_tool_timed_out(100.0, 700.0, 600)
    assert server_mod.silent_tool_timed_out(100.0, 900.0, 600)
    assert not server_mod.silent_tool_timed_out(100.0, 150.0, 600)


def test_silent_tool_timeout_env_override(monkeypatch):
    monkeypatch.delenv("PET_SILENT_TOOL_TIMEOUT", raising=False)
    assert server_mod._silent_tool_timeout_seconds() == 600

    monkeypatch.setenv("PET_SILENT_TOOL_TIMEOUT", "42.5")
    assert server_mod._silent_tool_timeout_seconds() == 42.5

    monkeypatch.setenv("PET_SILENT_TOOL_TIMEOUT", "not-a-number")
    assert server_mod._silent_tool_timeout_seconds() == 600

    monkeypatch.setenv("PET_SILENT_TOOL_TIMEOUT", "-1")
    assert server_mod._silent_tool_timeout_seconds() == 600


class _FakeClock:
    """Deterministic monotonic clock for the silent-streak tests."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _SlowMCP(_StubMCP):
    """Each tool call advances virtual time, like a slow real tool."""

    def __init__(self, clock: _FakeClock, seconds: float) -> None:
        super().__init__()
        self.clock = clock
        self.seconds = seconds

    async def call_tool(self, server_name, tool_name, arguments):
        self.called.append((server_name, tool_name, arguments))
        self.clock.advance(self.seconds)
        return {"summary": "【你记得的事】用户碰过你三次"}


class _TimedMCP(_StubMCP):
    """Per-call latency, so a test can shape the silent streak."""

    def __init__(self, clock: _FakeClock, delays: list[float]) -> None:
        super().__init__()
        self.clock = clock
        self.delays = list(delays)

    async def call_tool(self, server_name, tool_name, arguments):
        self.called.append((server_name, tool_name, arguments))
        if self.delays:
            self.clock.advance(self.delays.pop(0))
        return {"summary": "【你记得的事】用户碰过你三次"}


class _AlwaysRecallProvider:
    """Calls recall on every tool-enabled turn; answers only when tools=None."""

    name = "local"
    supports_function_calling = False

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.snapshots: list[list[dict]] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        self.snapshots.append([dict(m) for m in kwargs.get("messages", [])])
        if kwargs.get("tools") is None:
            yield {"type": "delta", "content": "翻到了：你碰过我三次。"}
        else:
            yield {
                "type": "tool_call",
                "id": "tc_recall",
                "server_name": "builtin",
                "name": "recall",
                "arguments": {"keyword": "用户"},
            }
        yield {"type": "end"}


def test_silent_tool_streak_forces_a_visible_tools_none_answer(monkeypatch):
    telemetry = Telemetry()
    monkeypatch.setattr(server_mod.telemetry, "_instance", telemetry)
    clock = _FakeClock()
    provider = _AlwaysRecallProvider()
    mcp = _SlowMCP(clock, server_mod.SILENT_TOOL_TIMEOUT_SECONDS + 50)

    chunks = _run_function_chat(
        provider,
        mcp,
        monkeypatch,
        [ToolDef(server_name="builtin", name="recall")],
        clock=clock,
    )
    frames = _frames(chunks)

    assert len(mcp.called) == 1, (
        "once the silent streak exceeds 10 minutes no further tool may run"
    )
    assert len(provider.calls) == 2, "the gateway must ask once more without tools"
    assert telemetry.snapshot()["counters"].get("chat.silent_tool_timeout") == 1
    catchup = provider.calls[-1]
    assert catchup.get("tools") is None, "the catch-up call must not expose tools"
    assert catchup.get("max_tokens") == 768
    prompt = json.dumps(catchup.get("messages", []), ensure_ascii=False, default=str)
    assert "连续调用工具" in prompt
    assert "10 分钟" in prompt
    assert "不要继续调用工具" in prompt

    text = _visible_text(frames)
    assert "翻到了" in text, "the forced final answer never reached the renderer"
    assert frames[-1]["event"] == "end"
    assert not any(frame["event"] == "error" for frame in frames)


def test_silent_timeout_skips_remaining_tools_in_the_same_round(monkeypatch):
    class _TwoToolCallsProvider:
        name = "local"
        supports_function_calling = False

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("tools") is None:
                yield {"type": "delta", "content": "先说到这里。"}
            else:
                for idx in (1, 2):
                    yield {
                        "type": "tool_call",
                        "id": f"tc_recall_{idx}",
                        "server_name": "builtin",
                        "name": "recall",
                        "arguments": {"keyword": f"用户{idx}"},
                    }
            yield {"type": "end"}

    telemetry = Telemetry()
    monkeypatch.setattr(server_mod.telemetry, "_instance", telemetry)
    clock = _FakeClock()
    provider = _TwoToolCallsProvider()
    mcp = _SlowMCP(clock, server_mod.SILENT_TOOL_TIMEOUT_SECONDS + 50)

    frames = _frames(
        _run_function_chat(
            provider,
            mcp,
            monkeypatch,
            [ToolDef(server_name="builtin", name="recall")],
            clock=clock,
        )
    )

    assert len(mcp.called) == 1, (
        "the clock is checked before EVERY tool: the slow first call used up "
        "the silent budget, so the second call must never run"
    )
    assert provider.calls[-1].get("tools") is None
    assert telemetry.snapshot()["counters"].get("chat.silent_tool_timeout") == 1
    assert "先说到这里" in _visible_text(frames)
    assert frames[-1]["event"] == "end"


def test_visible_speech_between_tools_prevents_truncation(monkeypatch):
    class _SpeakThenToolProvider:
        """工具→说话→工具→说话; total elapsed far exceeds the budget."""

        name = "local"
        supports_function_calling = False

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) <= 3:
                if len(self.calls) >= 2:
                    yield {
                        "type": "delta",
                        "content": f"我已经查了 {len(self.calls) - 1} 次。\n",
                    }
                yield {
                    "type": "tool_call",
                    "id": f"tc_recall_{len(self.calls)}",
                    "server_name": "builtin",
                    "name": "recall",
                    "arguments": {"keyword": "用户"},
                }
            else:
                yield {"type": "delta", "content": "全都查完了。"}
            yield {"type": "end"}

    telemetry = Telemetry()
    monkeypatch.setattr(server_mod.telemetry, "_instance", telemetry)
    clock = _FakeClock()
    provider = _SpeakThenToolProvider()
    # 3 tool calls × 500s = 1500s of total turn time, but every silent gap
    # between two visible replies is only 500s (< 600s).
    mcp = _SlowMCP(clock, 500)

    frames = _frames(
        _run_function_chat(
            provider,
            mcp,
            monkeypatch,
            [ToolDef(server_name="builtin", name="recall")],
            clock=clock,
        )
    )

    assert len(mcp.called) == 3
    assert len(provider.calls) == 4, (
        "visible speech between tool rounds must prevent any iteration cap"
    )
    assert clock.now - 1000.0 >= 2 * server_mod.SILENT_TOOL_TIMEOUT_SECONDS, (
        "the whole turn must outlast the budget for this test to be meaningful"
    )
    assert all(call.get("tools") is not None for call in provider.calls), (
        "no forced tools=None catch-up: the model answered on its own"
    )
    assert "chat.silent_tool_timeout" not in telemetry.snapshot()["counters"]
    text = _visible_text(frames)
    assert "我已经查了 2 次" in text
    assert "全都查完了" in text
    assert frames[-1]["event"] == "end"


def test_long_prefill_without_any_tool_never_triggers_silent_timeout(monkeypatch):
    """A thinking-only round is model work, not a silent tool streak.

    The old clock started at turn start, so a cold local prefill that took
    >10 minutes emitted nothing and then a normal answer — with the timeout
    already "expired". No tool call means no streak: never cut.
    """

    class _SlowPrefillProvider:
        name = "local"
        supports_function_calling = False

        def __init__(self, clock: _FakeClock) -> None:
            self.clock = clock
            self.calls: list[dict[str, Any]] = []

        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            # Cold prefill + long thinking, no tool involved at all.
            self.clock.advance(server_mod.SILENT_TOOL_TIMEOUT_SECONDS + 180)
            yield {"type": "think", "content": "慢慢想。"}
            yield {"type": "delta", "content": "想好了，就聊这个。"}
            yield {"type": "end"}

    telemetry = Telemetry()
    monkeypatch.setattr(server_mod.telemetry, "_instance", telemetry)
    clock = _FakeClock()
    provider = _SlowPrefillProvider(clock)

    frames = _frames(
        _run_function_chat(provider, _StubMCP(), monkeypatch, [], clock=clock)
    )

    assert len(provider.calls) == 1
    assert "chat.silent_tool_timeout" not in telemetry.snapshot()["counters"], (
        "a tool-less thinking round must never trigger the tool-silence cutoff"
    )
    assert "想好了" in _visible_text(frames)
    assert frames[-1]["event"] == "end"


def test_tool_streak_starts_at_the_first_tool_not_at_turn_start(monkeypatch):
    """Long prefill before tool #1 is free; consecutive tools are not."""

    class _SlowThinkThenToolProvider:
        name = "local"
        supports_function_calling = False

        def __init__(self, clock: _FakeClock) -> None:
            self.clock = clock
            self.calls: list[dict[str, Any]] = []

        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("tools") is None:
                yield {"type": "delta", "content": "查到了，先这样说。"}
            elif len(self.calls) == 1:
                # >budget of prefill/thinking, then the FIRST tool call.
                self.clock.advance(server_mod.SILENT_TOOL_TIMEOUT_SECONDS + 100)
                yield {"type": "think", "content": "想了非常久。"}
                yield {
                    "type": "tool_call",
                    "id": "tc_recall_1",
                    "server_name": "builtin",
                    "name": "recall",
                    "arguments": {"keyword": "用户"},
                }
            else:
                # Once the streak is armed, thinking between tool calls
                # counts against it.
                self.clock.advance(100)
                yield {"type": "think", "content": "还想再查一次。"}
                yield {
                    "type": "tool_call",
                    "id": "tc_recall_2",
                    "server_name": "builtin",
                    "name": "recall",
                    "arguments": {"keyword": "用户"},
                }
            yield {"type": "end"}

    telemetry = Telemetry()
    monkeypatch.setattr(server_mod.telemetry, "_instance", telemetry)
    clock = _FakeClock()
    provider = _SlowThinkThenToolProvider(clock)
    # 700s prefill → tool #1 armed at t+700, runs 400s (400 < 600).
    # Then 100s think + tool #2 (500 < 600 at execution, runs 400s);
    # 900s of consecutive tools with no speech → round-end timeout.
    mcp = _SlowMCP(clock, 400)

    frames = _frames(
        _run_function_chat(
            provider,
            mcp,
            monkeypatch,
            [ToolDef(server_name="builtin", name="recall")],
            clock=clock,
        )
    )

    assert len(mcp.called) == 2, (
        "the first tool must run after a long prefill, and the second one "
        "too (at 500s since the first tool)"
    )
    assert len(provider.calls) == 3, "two tool rounds, then one tools=None catch-up"
    assert provider.calls[-1].get("tools") is None
    assert telemetry.snapshot()["counters"].get("chat.silent_tool_timeout") == 1
    assert "连续调用工具" in json.dumps(
        provider.calls[-1].get("messages", []), ensure_ascii=False
    )
    assert "查到了" in _visible_text(frames)
    assert frames[-1]["event"] == "end"


def test_tool_status_think_and_mem_do_not_reset_the_tool_streak(monkeypatch):
    class _ChatterBetweenToolsProvider:
        name = "local"
        supports_function_calling = False

        def __init__(self, clock: _FakeClock) -> None:
            self.clock = clock
            self.calls: list[dict[str, Any]] = []

        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("tools") is None:
                yield {"type": "delta", "content": "不查了，我直接回答。"}
            elif len(self.calls) == 1:
                yield {
                    "type": "tool_call",
                    "id": "tc_recall_1",
                    "server_name": "builtin",
                    "name": "recall",
                    "arguments": {"keyword": "用户"},
                }
            else:
                # Between two consecutive tool calls the model streams only
                # pipeline chatter. None of it is speech, so the streak that
                # started before tool #1 must stay armed.
                self.clock.advance(200)
                yield {"type": "think", "content": "我再想想。"}
                yield {
                    "type": "delta",
                    "content": (
                        "\n_执行 recall..._\n"
                        '<<<MEM>>>{"memory":"x"}<<<MEMEND>>>\n'
                        "[EMOTION:neutral]\n"
                    ),
                }
                yield {
                    "type": "tool_call",
                    "id": "tc_recall_2",
                    "server_name": "builtin",
                    "name": "recall",
                    "arguments": {"keyword": "用户"},
                }
            yield {"type": "end"}

    telemetry = Telemetry()
    monkeypatch.setattr(server_mod.telemetry, "_instance", telemetry)
    clock = _FakeClock()
    provider = _ChatterBetweenToolsProvider(clock)
    # tool #1: 300s. Then 200s of chatter + tool #2 (500s since the arm,
    # still < 600) runs 200s → 700s since the arm → timeout. If any chatter
    # had reset the streak, tool #2 would be armed at t+500 and the turn
    # would keep looping instead.
    mcp = _TimedMCP(clock, [300, 200])

    frames = _frames(
        _run_function_chat(
            provider,
            mcp,
            monkeypatch,
            [ToolDef(server_name="builtin", name="recall")],
            clock=clock,
        )
    )

    assert len(mcp.called) == 2
    assert len(provider.calls) == 3
    assert provider.calls[-1].get("tools") is None
    assert telemetry.snapshot()["counters"].get("chat.silent_tool_timeout") == 1, (
        "think events, _执行 status asides and MEM blocks must not count as "
        "speech and must not re-arm the silent tool streak"
    )
    assert "不查了，我直接回答" in _visible_text(frames)
    assert frames[-1]["event"] == "end"


def test_silent_timeout_empty_catchup_emits_visible_fallback(monkeypatch):
    class _ToolThenSilenceProvider:
        name = "local"
        supports_function_calling = False

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("tools") is not None:
                yield {
                    "type": "tool_call",
                    "id": "tc_recall",
                    "server_name": "builtin",
                    "name": "recall",
                    "arguments": {"keyword": "用户"},
                }
            # tools=None catch-up returns nothing at all
            yield {"type": "end"}

    clock = _FakeClock()
    provider = _ToolThenSilenceProvider()
    chunks = _run_function_chat(
        provider,
        _SlowMCP(clock, server_mod.SILENT_TOOL_TIMEOUT_SECONDS + 50),
        monkeypatch,
        [ToolDef(server_name="builtin", name="recall")],
        clock=clock,
    )
    frames = _frames(chunks)

    text = _visible_text(frames)
    assert server_mod.TOOL_LOOP_FALLBACK_TEXT in text, (
        "an empty catch-up must still leave a visible delta, never a bare end"
    )
    assert frames[-1]["event"] == "end"


def test_recall_soft_limit_injects_a_stop_notice(monkeypatch):
    class _RecallThenAnswerProvider:
        """Ignores the soft hint once, then answers — proving it is not a cap."""

        name = "local"
        supports_function_calling = False

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.snapshots: list[list[dict]] = []

        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            self.snapshots.append([dict(m) for m in kwargs.get("messages", [])])
            if len(self.calls) >= 5:
                yield {"type": "delta", "content": "我把记得的都说了。"}
            else:
                yield {
                    "type": "tool_call",
                    "id": f"tc_recall_{len(self.calls)}",
                    "server_name": "builtin",
                    "name": "recall",
                    "arguments": {"keyword": "用户"},
                }
            yield {"type": "end"}

    telemetry = Telemetry()
    monkeypatch.setattr(server_mod.telemetry, "_instance", telemetry)
    provider = _RecallThenAnswerProvider()
    mcp = _StubMCP()

    _run_function_chat(
        provider,
        mcp,
        monkeypatch,
        [ToolDef(server_name="builtin", name="recall")],
        clock=_FakeClock(),  # frozen: soft hint only, no time-based cut
    )

    # snapshots[2] is the third model call (two recall results seen, no
    # notice yet); snapshots[3] is the first call after recall #3 executed.
    before = json.dumps(provider.snapshots[2], ensure_ascii=False, default=str)
    after = json.dumps(provider.snapshots[3], ensure_ascii=False, default=str)
    assert "不要再调用 recall" not in before
    assert "不要再调用 recall" in after, (
        "after 3 recall calls the model must be told to answer with what it has"
    )
    # Soft means soft: recall #4 still ran and the turn ended by the model
    # answering, not by a gateway cap.
    assert len(mcp.called) == 4
    assert len(provider.calls) == 5
    assert provider.calls[-1].get("tools") is not None
    assert "chat.silent_tool_timeout" not in telemetry.snapshot()["counters"]


def test_tool_loop_with_real_final_body_is_not_reanswered(monkeypatch):
    class _ToolThenAnswerProvider:
        name = "local"
        supports_function_calling = False

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                yield {
                    "type": "tool_call",
                    "id": "tc_recall",
                    "server_name": "builtin",
                    "name": "recall",
                    "arguments": {"keyword": "用户"},
                }
            else:
                yield {"type": "delta", "content": "想起来了，你碰过我三次。"}
            yield {"type": "end"}

    telemetry = Telemetry()
    monkeypatch.setattr(server_mod.telemetry, "_instance", telemetry)
    provider = _ToolThenAnswerProvider()
    chunks = _run_function_chat(
        provider,
        _StubMCP(),
        monkeypatch,
        [ToolDef(server_name="builtin", name="recall")],
    )
    frames = _frames(chunks)

    assert "chat.silent_tool_timeout" not in telemetry.snapshot()["counters"], (
        "a normal short tool round must never hit the silent timeout"
    )
    assert len(provider.calls) == 2, "a final body must not trigger a catch-up call"
    assert provider.calls[-1].get("tools") is not None
    prompt = json.dumps(provider.calls[-1].get("messages", []), ensure_ascii=False)
    assert "工具调用次数已用完" not in prompt
    assert "想起来了" in _visible_text(frames)
    assert frames[-1]["event"] == "end"


# ── DeepSeek thinking mode: reasoning_content must round-trip ──────────


class _FunctionCallProvider:
    """Function-calling provider (same shape as OpenAI/DeepSeek)."""

    name = "deepseek"
    supports_function_calling = True
    allow_tools = True

    def __init__(self, first_turn_events: list[dict]) -> None:
        self._first_turn_events = first_turn_events
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            for event in self._first_turn_events:
                yield event
        else:
            yield {"type": "delta", "content": "走过去了。"}
        yield {"type": "end"}


def _run_function_chat(provider, mcp, monkeypatch, tools, clock=None) -> list[bytes]:
    monkeypatch.setattr(server_mod, "_resolve_provider", lambda *a, **k: provider)
    monkeypatch.setattr(server_mod, "_kernel", lambda: None, raising=False)
    monkeypatch.setattr(server_mod, "get_event_store", lambda: None, raising=False)
    monkeypatch.setattr(server_mod, "_extraction_hook", None)
    if clock is not None:
        # Module-local indirection: does not disturb asyncio's own clock.
        monkeypatch.setattr(server_mod, "_monotonic", clock)

    async def _tools(_mcp_manager, _req, core_only=False):
        return tools

    monkeypatch.setattr(server_mod, "_gather_tools", _tools)

    req = ChatRequest(
        messages=[{"role": "user", "content": "走到那扇窗户旁边"}],
        tools_enabled=True,
    )

    async def _drain():
        return [
            chunk
            async for chunk in server_mod._stream_chat_provider(
                _StubRegistry(provider),
                mcp,
                _StubBridge(),
                req,
                server=None,
                server_state={},
                mood_store=_StubMood(),
                lora_arr=None,
            )
        ]

    return asyncio.run(_drain())


def _frames(chunks: list[bytes]) -> list[dict]:
    frames: list[dict] = []
    for chunk in chunks:
        text = chunk.decode("utf-8")
        assert text.startswith("data: "), "every SSE chunk must be one data frame"
        frames.append(json.loads(text[len("data: "):].strip()))
    return frames


def _visible_text(frames: list[dict]) -> str:
    return "".join(
        frame.get("content", "")
        for frame in frames
        if frame.get("event") == "delta"
    )


def _assistant_tool_messages(provider) -> list[dict]:
    follow_up = provider.calls[1]["messages"]
    return [
        m for m in follow_up
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]


def test_bare_tool_name_routes_to_its_server_once(monkeypatch):
    """`pet_world_walk_to` must run _world_tool, not mcp default/<name>."""
    provider = _FunctionCallProvider([{
        "type": "tool_call",
        "id": "tc_walk",
        "server_name": "default",
        "name": "pet_world_walk_to",
        "arguments": {"id": 7},
    }])
    mcp = _StubMCP()
    world_calls: list[tuple] = []

    async def _world_tool(name, args):
        world_calls.append((name, args))
        return "已走到刚体旁边。"

    monkeypatch.setattr(server_mod, "_world_tool", _world_tool)

    _run_function_chat(
        provider,
        mcp,
        monkeypatch,
        [ToolDef(server_name="pet-world", name="pet_world_walk_to")],
    )

    assert world_calls == [("pet_world_walk_to", {"id": 7})]
    assert mcp.called == [], "bare name must not fall through to default/<name>"


def test_bare_builtin_tool_name_routes_to_builtin_server(monkeypatch):
    provider = _FunctionCallProvider([{
        "type": "tool_call",
        "id": "tc_screen",
        "server_name": "default",
        "name": "observe_screen",
        "arguments": {},
    }])
    mcp = _StubMCP()

    _run_function_chat(
        provider,
        mcp,
        monkeypatch,
        [ToolDef(server_name="builtin", name="observe_screen")],
    )

    assert mcp.called == [("builtin", "observe_screen", {})], (
        "bare observe_screen must execute the builtin tool exactly once"
    )


def test_tool_loop_returns_reasoning_content_with_assistant_tool_call(monkeypatch):
    reasoning = "先找 id=7 的刚体，再决定站上去还是走到旁边。"
    provider = _FunctionCallProvider([{
        "type": "tool_call",
        "id": "tc_walk",
        "server_name": "default",
        "name": "pet_world_walk_to",
        "arguments": {"id": 7},
        "reasoning_content": reasoning,
    }])
    mcp = _StubMCP()

    async def _world_tool(_name, _args):
        return "已走到刚体旁边。"

    monkeypatch.setattr(server_mod, "_world_tool", _world_tool)

    _run_function_chat(
        provider,
        mcp,
        monkeypatch,
        [ToolDef(server_name="pet-world", name="pet_world_walk_to")],
    )

    assistant = _assistant_tool_messages(provider)
    assert len(assistant) == 1
    assert assistant[0]["reasoning_content"] == reasoning, (
        "DeepSeek thinking mode rejects the follow-up round unless the "
        "assistant tool_calls message carries reasoning_content"
    )


def test_tool_loop_omits_reasoning_content_when_absent(monkeypatch):
    provider = _FunctionCallProvider([{
        "type": "tool_call",
        "id": "tc_walk",
        "server_name": "default",
        "name": "pet_world_walk_to",
        "arguments": {"id": 7},
    }])
    mcp = _StubMCP()

    async def _world_tool(_name, _args):
        return "已走到刚体旁边。"

    monkeypatch.setattr(server_mod, "_world_tool", _world_tool)

    _run_function_chat(
        provider,
        mcp,
        monkeypatch,
        [ToolDef(server_name="pet-world", name="pet_world_walk_to")],
    )

    assistant = _assistant_tool_messages(provider)
    assert len(assistant) == 1
    assert "reasoning_content" not in assistant[0]


# ── Text [MCP:] path: the server reconstructs the assistant turn ────────


_MCP_TEXT = '[MCP:builtin/recall:{"keyword":"用户"}]'


def test_text_mcp_tool_call_carries_this_rounds_reasoning(monkeypatch):
    """A text tool call is rebuilt by the server, so the round's think text
    must be attached by hand — DeepSeek rejects the follow-up otherwise."""
    provider = _FunctionCallProvider([
        {"type": "think", "content": "先回忆"},
        {"type": "think", "content": "一下。"},
        {"type": "delta", "content": _MCP_TEXT},
    ])
    mcp = _StubMCP()

    _run_function_chat(
        provider,
        mcp,
        monkeypatch,
        [ToolDef(server_name="builtin", name="recall")],
    )

    assert mcp.called == [("builtin", "recall", {"keyword": "用户"})]
    assistant = _assistant_tool_messages(provider)
    assert len(assistant) == 1
    assert assistant[0]["reasoning_content"] == "先回忆一下。"


def test_text_mcp_tool_call_omits_reasoning_without_think(monkeypatch):
    provider = _FunctionCallProvider([
        {"type": "delta", "content": _MCP_TEXT},
    ])
    mcp = _StubMCP()

    _run_function_chat(
        provider,
        mcp,
        monkeypatch,
        [ToolDef(server_name="builtin", name="recall")],
    )

    assistant = _assistant_tool_messages(provider)
    assert len(assistant) == 1
    assert "reasoning_content" not in assistant[0], (
        "no think events this round → no reasoning_content key at all"
    )


def test_native_tool_call_reasoning_is_not_doubled_with_think_events(monkeypatch):
    """Native tool calls already carry reasoning_content (provider-emitted).
    Server-side round accumulation must apply only to the text path."""
    native_reasoning = "原生理由"
    provider = _FunctionCallProvider([
        {"type": "think", "content": "流式思考"},
        {
            "type": "tool_call",
            "id": "tc_walk",
            "server_name": "default",
            "name": "pet_world_walk_to",
            "arguments": {"id": 7},
            "reasoning_content": native_reasoning,
        },
    ])
    mcp = _StubMCP()

    async def _world_tool(_name, _args):
        return "已走到刚体旁边。"

    monkeypatch.setattr(server_mod, "_world_tool", _world_tool)

    _run_function_chat(
        provider,
        mcp,
        monkeypatch,
        [ToolDef(server_name="pet-world", name="pet_world_walk_to")],
    )

    assistant = _assistant_tool_messages(provider)
    assert len(assistant) == 1
    assert assistant[0]["reasoning_content"] == native_reasoning


def test_build_tool_map_registers_unique_bare_names_only():
    tools = [
        ToolDef(server_name="pet-world", name="pet_world_walk_to"),
        ToolDef(server_name="builtin", name="observe_screen"),
        ToolDef(server_name="builtin", name="memory"),
        ToolDef(server_name="pet", name="memory"),
    ]

    tool_map = server_mod._build_tool_map(tools)

    assert tool_map["pet_world_walk_to"] == ("pet-world", "pet_world_walk_to")
    assert tool_map["observe_screen"] == ("builtin", "observe_screen")
    assert tool_map["mcp_builtin_memory"] == ("builtin", "memory")
    assert tool_map["mcp_pet_memory"] == ("pet", "memory")
    assert "memory" not in tool_map, (
        "a duplicate bare name is ambiguous and must keep only prefixed forms"
    )


class _SSEHandler:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __call__(self, _request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=self.body,
            headers={"content-type": "text/event-stream"},
        )


def _sse(obj: dict) -> bytes:
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


def test_openai_provider_accumulates_reasoning_for_tool_call():
    body = b"".join([
        _sse({"choices": [{"index": 0, "delta": {"reasoning_content": "先想"}}]}),
        _sse({"choices": [{"index": 0, "delta": {"reasoning_content": "一下"}}]}),
        _sse({"choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0,
            "id": "call_1",
            "function": {
                "name": "mcp_pet_world_pet_world_walk_to",
                "arguments": '{"id":',
            },
        }]}}]}),
        _sse({"choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0,
            "function": {"arguments": "7}"},
        }]}}]}),
        _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
        b"data: [DONE]\n\n",
    ])
    provider = OpenAIProvider(
        api_key="",
        base_url="http://t",
        model="deepseek-chat",
        name="deepseek",
        thinking=True,
    )
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_SSEHandler(body)),
        base_url="http://t",
    )

    async def _collect():
        try:
            return [
                event
                async for event in provider.chat(
                    [{"role": "user", "content": "走到窗户旁"}],
                    tools=[ToolDef(server_name="pet-world", name="pet_world_walk_to")],
                )
            ]
        finally:
            await provider.close()

    events = asyncio.run(_collect())
    tool_events = [e for e in events if e["type"] == "tool_call"]

    assert len(tool_events) == 1
    assert tool_events[0]["reasoning_content"] == "先想一下"
    assert tool_events[0]["arguments"] == {"id": 7}
    assert not any(e["type"] == "delta" for e in events), (
        "reasoning must stay in think events, never in visible deltas"
    )


# ── DeepSeek reasoning_content 400: one-shot thinking downgrade ─────────


_REASONING_400 = (
    "The reasoning_content in the thinking mode must be passed back to the API."
)


def test_is_reasoning_content_error_only_matches_thinking_mode_400():
    assert _is_reasoning_content_error(_REASONING_400) is True
    assert _is_reasoning_content_error('{"error":{"message":"%s"}}' % _REASONING_400) is True
    assert _is_reasoning_content_error("Unsupported parameter: thinking") is False
    assert _is_reasoning_content_error("invalid api key") is False
    assert _is_reasoning_content_error("") is False


class _Reasoning400ThenOK:
    """First request rejects thinking mode; the retry must succeed."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.bodies.append(body)
        if len(self.bodies) == 1:
            return httpx.Response(400, json={"error": {"message": _REASONING_400}})
        return httpx.Response(
            200,
            content=_sse({"choices": [{"index": 0, "delta": {"content": "降级成功"}}]}),
            headers={"content-type": "text/event-stream"},
        )


def test_reasoning_content_400_retries_without_thinking_once():
    transport = _Reasoning400ThenOK()
    provider = OpenAIProvider(
        api_key="",
        base_url="http://t",
        model="deepseek-v4-flash",
        name="deepseek",
        thinking=True,
        reasoning_effort="max",
    )
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(transport),
        base_url="http://t",
    )

    async def _collect():
        try:
            return [
                event
                async for event in provider.chat(
                    [{"role": "user", "content": "在吗"}],
                )
            ]
        finally:
            await provider.close()

    events = asyncio.run(_collect())

    assert len(transport.bodies) == 2, "exactly one downgrade retry is allowed"
    assert transport.bodies[0]["thinking"] == {"type": "enabled"}
    assert transport.bodies[0]["reasoning_effort"] == "max"
    assert "thinking" not in transport.bodies[1]
    assert "reasoning_effort" not in transport.bodies[1]
    assert provider._thinking_unsupported is True
    assert any(e["type"] == "delta" and e["content"] == "降级成功" for e in events)
    assert not any(e["type"] == "error" for e in events)


def test_reasoning_content_400_gives_up_after_one_retry():
    class _Always400:
        def __init__(self) -> None:
            self.bodies: list[dict] = []

        async def __call__(self, request: httpx.Request) -> httpx.Response:
            self.bodies.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(400, json={"error": {"message": _REASONING_400}})

    transport = _Always400()
    provider = OpenAIProvider(
        api_key="",
        base_url="http://t",
        model="deepseek-v4-flash",
        name="deepseek",
        thinking=True,
    )
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(transport),
        base_url="http://t",
    )

    async def _collect():
        try:
            return [
                event
                async for event in provider.chat(
                    [{"role": "user", "content": "在吗"}],
                )
            ]
        finally:
            await provider.close()

    events = asyncio.run(_collect())

    assert len(transport.bodies) == 2, "must not loop on the same 400"
    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1 and "400" in errors[0]["message"]
