"""Gateway chat-path budget contract (post Laya pre-call removal).

The per-turn Laya budget pre-call is obsolete: the default chat path is
"fast intuition" (a no-thinking LLM). These tests drive the real
``_stream_chat_provider`` wiring with a scripted OpenAIProvider subclass and
pin the new contract:

  * no Laya router call happens on any chat turn;
  * ``req.thinking`` / ``req.reasoning_effort`` are always forwarded to
    OpenAI-compatible providers, so static providers.json defaults
    (thinking=true, effort=max) cannot force slow thinking;
  * the cloud 8192 output floor is legacy behaviour again (no routed
    budget ever overrides it);
  * ``req.max_new_tokens`` is never mutated by the gateway;
  * no ``decision.budget.*`` telemetry is produced.

``_output_budget``'s optional ``decision`` argument is kept for future use,
so its unit tests stay here. The provider-body override tests at the bottom
pin how the overrides interact with static provider defaults.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from gateway import server as server_mod
from gateway.decision import laya_router
from gateway.decision.laya_router import BudgetDecision
from gateway.providers.openai import OpenAIProvider
from gateway.server import ChatRequest, _output_budget
from gateway.telemetry import Telemetry


# ── Fixtures / stubs ────────────────────────────────────────────────


class _RecordingOpenAI(OpenAIProvider):
    """Real OpenAIProvider surface, no HTTP: records the kwargs it gets."""

    def __init__(self, **kwargs) -> None:
        super().__init__(
            base_url="https://api.example.com/v1",
            model="deepseek-chat",
            name="deepseek",
            thinking=True,
            reasoning_effort="max",
            **kwargs,
        )
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        yield {"type": "delta", "content": "好"}
        yield {"type": "end"}


class _StubMCP:
    async def call_tool(self, server_name, tool_name, arguments):
        return {"summary": "ok"}


class _StubBridge:
    def new_session(self) -> None: ...

    def post(self, *_a, **_k) -> None: ...


class _StubMood:
    emotion_index = -0.2

    def format_for_system_prompt(self) -> str:
        return ""

    def apply_emotion_tag(self, _tag) -> None: ...


class _StubRegistry:
    def __init__(self, provider) -> None:
        self._providers = {provider.name: provider}
        self._default = provider

    def get(self, name):
        return self._providers.get(name, self._default)


# A prompt that keeps ``decide_thinking`` (the meta seat, kept intact) at
# thinking=True: code fence (+0.4) + 报错 (+0.4) + question mark (+0.1).
_THINKING_PROMPT = "```python\nraise KeyError('x')\n```\n为什么这段代码会报错？"


def _decision(
    tier: str,
    thinking: bool,
    effort: str,
    tokens: int,
    *,
    source: str = "laya",
    confidence: float = 0.9,
) -> BudgetDecision:
    return BudgetDecision(
        tier=tier,
        thinking=thinking,
        reasoning_effort=effort,
        max_tokens=tokens,
        source=source,
        confidence=confidence,
        reason=f"{source}:{tier}",
    )


def _forbid_laya(monkeypatch) -> list[tuple[str, tuple, dict]]:
    """Make any Laya router call fail loudly and record that it happened.

    The old gateway wiring caught router exceptions, so a bare raise could be
    swallowed. Recording first keeps the "never called" assertion honest even
    if the exception is handled.
    """
    calls: list[tuple[str, tuple, dict]] = []

    async def _no_budget(*args, **kwargs):
        calls.append(("decide_budget", args, kwargs))
        raise AssertionError("laya_router.decide_budget must not be called")

    def _no_rules(*args, **kwargs):
        calls.append(("decide_by_rules", args, kwargs))
        raise AssertionError("laya_router.decide_by_rules must not be called")

    monkeypatch.setattr(laya_router, "decide_budget", _no_budget)
    monkeypatch.setattr(laya_router, "decide_by_rules", _no_rules)
    return calls


def _run_chat(monkeypatch, provider, req):
    """Drive one real turn through the gateway's provider stream."""

    router_calls = _forbid_laya(monkeypatch)

    monkeypatch.setattr(server_mod, "_resolve_provider", lambda *_a, **_k: provider)
    monkeypatch.setattr(server_mod, "_kernel", lambda: None)
    monkeypatch.setattr(server_mod, "get_event_store", lambda: None)
    monkeypatch.setattr(server_mod, "_extraction_hook", None)

    async def _no_tools(*_a, **_k):
        return None

    monkeypatch.setattr(server_mod, "_gather_tools", _no_tools)

    async def _drain():
        return [
            chunk
            async for chunk in server_mod._stream_chat_provider(
                _StubRegistry(provider),
                _StubMCP(),
                _StubBridge(),
                req,
                server=None,
                server_state={},
                mood_store=_StubMood(),
                lora_arr=None,
            )
        ]

    return asyncio.run(_drain()), router_calls


def _req(**kwargs) -> ChatRequest:
    defaults = {
        "messages": [{"role": "user", "content": "今天怎么样"}],
        "max_new_tokens": 4096,
        "thinking": False,
        "tools_enabled": False,
    }
    defaults.update(kwargs)
    return ChatRequest(**defaults)


# ── _output_budget (optional decision kept for future use) ──────────


class _Cloud:
    name = "deepseek"
    is_local = False


class _Local:
    name = "local"
    is_local = True


@pytest.mark.parametrize(
    "tier,thinking,effort,tokens",
    [
        ("fast", False, "low", 512),
        ("normal", False, "low", 1024),
        ("deep", True, "high", 2048),
        ("max", True, "max", 4096),
    ],
)
def test_output_budget_uses_decision_instead_of_cloud_8192(
    tier, thinking, effort, tokens
):
    decision = _decision(tier, thinking, effort, tokens)
    assert _output_budget(_Cloud(), _req(), decision) == tokens
    assert _output_budget(_Cloud(), _req(), decision) < 8192


def test_output_budget_keeps_absolute_cap_and_legacy_path():
    huge = _decision("max", True, "max", 99999)
    assert _output_budget(_Cloud(), _req(), huge) == server_mod.MAX_NEW_TOKENS_CAP
    # No decision = legacy behaviour (the only path the chat wiring uses).
    assert _output_budget(_Cloud(), _req()) == 8192
    assert _output_budget(_Local(), _req(max_new_tokens=768)) == 768


# ── Chat path: no Laya pre-call ─────────────────────────────────────


def test_chat_path_never_calls_laya_router(monkeypatch):
    provider = _RecordingOpenAI()
    frames, router_calls = _run_chat(monkeypatch, provider, _req())

    assert router_calls == []
    assert any(b'"event": "end"' in frame for frame in frames)
    assert not any(b'"event": "error"' in frame for frame in frames)


def test_silent_turn_never_calls_laya_router(monkeypatch):
    provider = _RecordingOpenAI()
    _frames, router_calls = _run_chat(
        monkeypatch, provider, _req(silent=True, thinking=False)
    )

    assert router_calls == []


# ── Thinking / effort overrides always reach the provider ───────────


def test_thinking_false_reaches_provider_over_static_defaults(monkeypatch):
    # Static providers.json defaults are thinking=True, effort=max.
    provider = _RecordingOpenAI()
    req = _req(thinking=False)

    _run_chat(monkeypatch, provider, req)

    call = provider.calls[0]
    assert call["thinking_override"] is False
    assert call["reasoning_effort_override"] is None


def test_thinking_true_forwards_requested_effort(monkeypatch):
    provider = _RecordingOpenAI()
    req = _req(
        messages=[{"role": "user", "content": _THINKING_PROMPT}],
        thinking=True,
        reasoning_effort="high",
    )

    _run_chat(monkeypatch, provider, req)

    call = provider.calls[0]
    assert call["thinking_override"] is True
    assert call["reasoning_effort_override"] == "high"
    assert req.thinking is True


# ── Legacy cloud floor / request immutability ───────────────────────


@pytest.mark.parametrize("thinking", [False, True])
def test_cloud_turn_keeps_legacy_8192_floor(monkeypatch, thinking):
    provider = _RecordingOpenAI()
    req = _req(
        messages=[{"role": "user", "content": _THINKING_PROMPT}],
        thinking=thinking,
        max_new_tokens=4096,
    )

    _run_chat(monkeypatch, provider, req)

    assert provider.calls[0]["max_tokens"] == 8192


@pytest.mark.parametrize("thinking,effort", [(False, None), (True, "high")])
def test_request_max_new_tokens_is_not_overwritten(monkeypatch, thinking, effort):
    provider = _RecordingOpenAI()
    req = _req(
        messages=[{"role": "user", "content": _THINKING_PROMPT}],
        thinking=thinking,
        reasoning_effort=effort,
        max_new_tokens=2048,
    )

    _run_chat(monkeypatch, provider, req)

    assert req.max_new_tokens == 2048
    assert req.thinking is thinking
    assert req.reasoning_effort == effort


# ── Telemetry: no decision.budget counters ──────────────────────────


def test_no_decision_budget_telemetry(monkeypatch):
    telemetry = Telemetry()
    monkeypatch.setattr(server_mod.telemetry, "_instance", telemetry)

    _run_chat(monkeypatch, _RecordingOpenAI(), _req(thinking=False))

    snapshot = telemetry.snapshot()
    keys = [*snapshot["counters"], *snapshot["gauges"]]
    assert [key for key in keys if key.startswith("decision.budget")] == []


# ── OpenAIProvider body: request overrides beat providers.json ──────


class _BodyRecorder:
    def __init__(self) -> None:
        self.bodies: list[dict] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content.decode("utf-8")))
        payload = json.dumps({"choices": [{"index": 0, "delta": {"content": "ok"}}]})
        body = f"data: {payload}\n\ndata: [DONE]\n\n".encode("utf-8")
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )


def _run_provider(provider: OpenAIProvider, **chat_kwargs) -> None:
    async def _collect():
        try:
            return [
                event
                async for event in provider.chat(
                    [{"role": "user", "content": "在吗"}], **chat_kwargs
                )
            ]
        finally:
            await provider.close()

    asyncio.run(_collect())


def _provider_with_recorder(**provider_kwargs) -> tuple[OpenAIProvider, _BodyRecorder]:
    recorder = _BodyRecorder()
    kwargs = {
        "base_url": "http://t",
        "model": "deepseek-chat",
        "name": "deepseek",
        "thinking": True,
        "reasoning_effort": "max",
    }
    kwargs.update(provider_kwargs)
    provider = OpenAIProvider(**kwargs)
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder), base_url=kwargs["base_url"]
    )
    return provider, recorder


def test_openai_provider_override_false_sends_explicit_disabled():
    # Omitting `thinking` used to be treated as "off", but DeepSeek V4
    # defaults reasoning on; the docs require {"type": "disabled"}.
    provider, recorder = _provider_with_recorder()
    _run_provider(provider, thinking_override=False, reasoning_effort_override=None)

    body = recorder.bodies[0]
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in body


def test_openai_provider_override_false_on_local_sends_no_thinking():
    # LM Studio / llama.cpp reject unknown fields, so a loopback endpoint
    # must never receive the disabled marker even with thinking=True.
    provider, recorder = _provider_with_recorder(base_url="http://127.0.0.1:1234/v1")
    _run_provider(provider, thinking_override=False, reasoning_effort_override=None)

    body = recorder.bodies[0]
    assert "thinking" not in body
    assert "reasoning_effort" not in body


def test_openai_provider_override_false_with_unknown_support_sends_no_thinking():
    # thinking=None means "unknown"; omission keeps working providers happy.
    provider, recorder = _provider_with_recorder(thinking=None)
    _run_provider(provider, thinking_override=False, reasoning_effort_override=None)

    body = recorder.bodies[0]
    assert "thinking" not in body
    assert "reasoning_effort" not in body


def test_openai_provider_override_false_suppressed_when_thinking_unsupported():
    # After a 400 "Unsupported parameter: thinking" the field stays absent.
    provider, recorder = _provider_with_recorder()
    provider._thinking_unsupported = True
    _run_provider(provider, thinking_override=False, reasoning_effort_override=None)

    body = recorder.bodies[0]
    assert "thinking" not in body
    assert "reasoning_effort" not in body


def test_openai_provider_override_true_sends_enabled_and_effort():
    provider, recorder = _provider_with_recorder()
    _run_provider(provider, thinking_override=True, reasoning_effort_override="high")

    body = recorder.bodies[0]
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"


def test_openai_provider_none_override_keeps_static_defaults():
    provider, recorder = _provider_with_recorder()
    _run_provider(provider)

    body = recorder.bodies[0]
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "max"
