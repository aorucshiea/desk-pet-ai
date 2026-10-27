"""DecisionFrame V1 tests -- no real Laya sidecar involved.

Pins the contract that the gateway wiring will rely on:
  * one /v1/systemone request carries six legal questions;
  * choice/noul answers are parsed into a frame;
  * short text, timeouts, bad payloads and low confidence fall back to rules;
  * a deep/max answer is turn-scoped through DepthState, never sticky;
  * the deterministic fallback produces sensible intent/risk/tone/depth;
  * frame_to_budget maps fast/low/deep/max and applies tool/mood floors.
"""

from __future__ import annotations

import json

import httpx
import pytest

from gateway.decision.frame import (
    DecisionFrame,
    DepthState,
    FRAME_CONFIDENCE_THRESHOLD,
    clear_cache,
    decide_by_rules_frame,
    decide_frame,
    frame_to_budget,
)

_INTENT_OPTIONS = ("chat", "recall", "search", "observe", "move", "remember")
_DEPTH_OPTIONS = ("fast", "low", "deep", "max")
_RISK_OPTIONS = ("safe", "caution", "deny")
_TONE_OPTIONS = ("calm", "warm", "playful", "serious", "sad")


@pytest.fixture(autouse=True)
def _clean_frame_cache(monkeypatch):
    monkeypatch.delenv("PET_LAYA_URL", raising=False)
    monkeypatch.delenv("PET_LAYA_TIMEOUT", raising=False)
    monkeypatch.delenv("PET_LAYA_ENABLED", raising=False)
    clear_cache()
    yield
    clear_cache()


def _choice(value: str, options: tuple[str, ...], confidence: float = 0.9) -> dict:
    other = (1.0 - confidence) / (len(options) - 1)
    probabilities = {option: other for option in options}
    probabilities[value] = confidence
    return {
        "type": "choice",
        "choice": value,
        "probabilities": probabilities,
        "confidence": 0.5,  # normalized entropy; not the selected probability
        "answer_confidence": confidence,
    }


def _noul(p_true: float = 0.9, confidence: float | None = None) -> dict:
    return {
        "type": "noul",
        "noul": p_true,
        "confidence": max(p_true, 1.0 - p_true),
        "answer_confidence": (
            max(p_true, 1.0 - p_true) if confidence is None else confidence
        ),
    }


def _answer(
    *,
    attend: float = 0.9,
    speak: float = 0.9,
    intent: str = "chat",
    depth: str = "low",
    risk: str = "safe",
    tone: str = "calm",
    confidence: float = 0.9,
) -> dict:
    return {
        "answers": {
            "attend": _noul(attend),
            "speak": _noul(speak),
            "intent": _choice(intent, _INTENT_OPTIONS, confidence),
            "depth": _choice(depth, _DEPTH_OPTIONS, confidence),
            "risk": _choice(risk, _RISK_OPTIONS, confidence),
            "tone": _choice(tone, _TONE_OPTIONS, confidence),
        },
        "usage": {"input_tokens": 42, "output_tokens": 0},
    }


def _handler_for(payload: dict, bodies: list[dict] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if bodies is not None:
            bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json=payload)

    return handler


# ── Request contract ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_request_contains_six_legal_questions():
    seen: dict = {}
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode("utf-8"))
        bodies.append(seen["body"])
        return httpx.Response(200, json=_answer())

    text = "今天天气怎么样？"
    frame = await decide_frame(text, transport=httpx.MockTransport(handler))

    assert frame.source == "laya"
    assert seen["url"] == "http://127.0.0.1:8765/v1/systemone"
    assert len(bodies) == 1

    body = seen["body"]
    assert body["model"] == "zh-tier"
    assert body["state"] == text
    questions = body["questions"]
    assert list(questions) == ["attend", "speak", "intent", "depth", "risk", "tone"]

    for key in ("attend", "speak"):
        question = questions[key]
        assert question["type"] == "noul"
        assert question["instructions"]
        assert set(question["criteria"]) == {"true", "false"}

    expected = {
        "intent": set(_INTENT_OPTIONS),
        "depth": set(_DEPTH_OPTIONS),
        "risk": set(_RISK_OPTIONS),
        "tone": set(_TONE_OPTIONS),
    }
    for key, options in expected.items():
        question = questions[key]
        assert question["type"] == "choice"
        assert question["instructions"]
        assert set(question["criteria"]) == options


@pytest.mark.asyncio
async def test_parse_choice_and_noul_answers():
    payload = _answer(
        attend=0.93,
        speak=0.12,  # P(true)=0.12 -> confident false
        intent="recall",
        depth="deep",
        risk="caution",
        tone="warm",
        confidence=0.8,
    )
    frame = await decide_frame(
        "你还记得上次我们聊的那个方案吗",
        transport=httpx.MockTransport(_handler_for(payload)),
    )

    assert frame.source == "laya"
    assert frame.attend is True
    assert frame.speak is False
    assert frame.intent == "recall"
    assert frame.depth == "deep"
    assert frame.risk == "caution"
    assert frame.tone == "warm"
    assert frame.confidence == pytest.approx(0.8)
    assert frame.reason.startswith("laya:")
    assert "speak=False" in frame.reason
    assert set(frame.questions_raw) == {
        "attend",
        "speak",
        "intent",
        "depth",
        "risk",
        "tone",
    }


@pytest.mark.asyncio
async def test_low_confidence_uses_neutral_rules_defaults():
    payload = _answer(intent="move", depth="max", risk="deny", tone="playful")
    for answer in payload["answers"].values():
        answer["answer_confidence"] = FRAME_CONFIDENCE_THRESHOLD - 0.2

    frame = await decide_frame(
        "今天过得怎么样",
        transport=httpx.MockTransport(_handler_for(payload)),
    )

    assert frame.source == "rules"
    assert frame.depth == "low"
    assert frame.intent == "chat"
    assert frame.risk == "safe"
    assert frame.tone == "calm"
    assert frame.attend is True
    assert frame.speak is True
    assert "low_confidence" in frame.reason


# ── Bad responses fall back to rules ────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,payload,reason_part",
    [
        (503, {"error": "busy"}, "laya_http_503"),
        (200, b"{not json", "laya_invalid_json"),
        (200, {"answers": []}, "laya_invalid_payload"),
        (
            200,
            {"answers": {"attend": {"type": "noul", "noul": "yes"}}},
            "laya_invalid_noul",
        ),
        (
            200,
            {"answers": [{"attend": 1}]},  # wrong container
            "laya_invalid_payload",
        ),
    ],
)
async def test_bad_sidecar_responses_fall_back_to_rules(status, payload, reason_part):
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(payload, bytes):
            return httpx.Response(status, content=payload)
        return httpx.Response(status, json=payload)

    frame = await decide_frame("早上好呀", transport=httpx.MockTransport(handler))

    assert frame.source == "rules"
    assert frame.depth == "fast"  # rules: greeting
    assert reason_part in frame.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate,reason_part",
    [
        (
            lambda p: p["answers"].pop("tone"),
            "laya_invalid_payload:tone",
        ),
        (
            lambda p: p["answers"]["intent"].update(choice="dance"),
            "laya_invalid_choice",
        ),
        (
            lambda p: p["answers"]["depth"].update(
                {"choice": "deep", "answer_confidence": "high"}
            )
            or p["answers"]["depth"].pop("probabilities", None),
            "laya_invalid_confidence",
        ),
    ],
)
async def test_malformed_answers_fall_back_to_rules(mutate, reason_part):
    payload = _answer()
    mutate(payload)

    frame = await decide_frame(
        "早上好呀",
        transport=httpx.MockTransport(_handler_for(payload)),
    )

    assert frame.source == "rules"
    assert reason_part in frame.reason


@pytest.mark.asyncio
async def test_timeout_falls_back_to_rules():
    import asyncio

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=_answer(depth="max"))

    frame = await decide_frame(
        "早上好呀",
        transport=httpx.MockTransport(handler),
        timeout=0.01,
    )

    assert frame.source == "rules"
    assert frame.depth == "fast"
    assert frame.reason.startswith("laya_timeout|")


@pytest.mark.asyncio
async def test_transport_error_falls_back_to_rules():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sidecar down", request=request)

    frame = await decide_frame(
        "早上好呀",
        transport=httpx.MockTransport(handler),
    )

    assert frame.source == "rules"
    assert frame.depth == "fast"
    assert frame.reason.startswith("laya_error:ConnectError|")


# ── Short text / cache ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_short_text_never_calls_sidecar_and_is_fast():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_answer(depth="max"))

    transport = httpx.MockTransport(handler)
    for text in ("", "   ", "嗯", "你好", "ok"):
        frame = await decide_frame(text, has_tools=True, transport=transport)
        assert frame.source == "short"
        assert frame.depth == "fast"
        assert frame.intent == "chat"
        assert frame.risk == "safe"
        assert frame.tone == "calm"
        assert frame.attend is True and frame.speak is True
        assert frame.confidence == 1.0
        assert frame.reason == "short_text"

    assert calls == 0


@pytest.mark.asyncio
async def test_cache_hit_sends_one_request():
    bodies: list[dict] = []
    payload = _answer(intent="chat", depth="low", risk="safe", tone="calm")

    transport = httpx.MockTransport(_handler_for(payload, bodies))
    text = "今天天气怎么样，适合出去散步吗？"

    first = await decide_frame(text, transport=transport)
    second = await decide_frame(f"  {text}  ", transport=transport)

    assert first.source == "laya"
    assert second.source == "cache"
    assert (second.intent, second.depth) == (first.intent, first.depth)
    assert second.risk == first.risk
    assert second.tone == first.tone
    assert second.confidence == first.confidence
    assert len(bodies) == 1


@pytest.mark.asyncio
async def test_rules_fallback_is_not_cached():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": "busy"})
        return httpx.Response(200, json=_answer(intent="recall", depth="deep"))

    transport = httpx.MockTransport(handler)
    text = "你还记得上次我们聊的那个方案吗"

    first = await decide_frame(text, transport=transport)
    second = await decide_frame(text, transport=transport)

    assert first.source == "rules"
    assert second.source == "laya"
    assert second.intent == "recall"
    assert calls == 2


# ── DepthState ──────────────────────────────────────────────────────


def test_depth_state_defaults_to_low_and_is_turn_scoped():
    state = DepthState()
    assert state.current() == "low"

    assert state.begin_turn() == "low"
    assert state.current() == "low"

    assert state.offer("max") == "max"
    assert state.current() == "max"
    assert state.offer("deep") == "max"  # never downgrade inside one turn
    assert state.finish_turn() == "low"
    assert state.current() == "low"


def test_depth_state_deep_is_one_turn_only():
    state = DepthState()
    state.begin_turn()
    assert state.offer("deep") == "deep"
    assert state.offer("max") == "max"  # upgrade is allowed
    assert state.finish_turn() == "low"
    assert state.current() == "low"

    # A new turn starts low even after a previous max turn.
    assert state.begin_turn() == "low"
    assert state.current() == "low"


def test_depth_state_fast_and_low_never_escalate():
    state = DepthState()
    state.begin_turn()
    assert state.offer("fast") == "low"
    assert state.current() == "low"
    assert state.offer("low") == "low"
    assert state.begin_turn() == "low"
    assert state.current() == "low"


def test_depth_state_offer_without_a_turn_stays_low():
    state = DepthState()
    assert state.offer("max") == "low"
    assert state.offer("deep") == "low"
    assert state.current() == "low"


def test_depth_state_uses_injected_clock():
    clock = {"now": 1000.0}
    state = DepthState(clock=lambda: clock["now"])

    assert state.started_at is None
    state.begin_turn()
    assert state.started_at == 1000.0

    clock["now"] = 1001.5
    state.finish_turn()
    assert state.started_at is None


@pytest.mark.asyncio
async def test_decide_frame_feeds_depth_state_and_finish_resets():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if "架构" in body["state"]:
            return httpx.Response(200, json=_answer(depth="max"))
        return httpx.Response(200, json=_answer(depth="low"))

    transport = httpx.MockTransport(handler)
    state = DepthState()

    deep = await decide_frame("帮我设计一个完整的系统架构方案", state=state, transport=transport)
    assert deep.depth == "max"
    assert state.current() == "max"

    assert state.finish_turn() == "low"
    assert state.current() == "low"

    low = await decide_frame("今天天气怎么样？", state=state, transport=transport)
    assert low.depth == "low"
    assert state.current() == "low"


# ── Rules fallback semantics ────────────────────────────────────────


@pytest.mark.parametrize(
    "text,emotion,has_tools,intent,risk,tone,depth",
    [
        ("早上好呀", 0.0, False, "chat", "safe", "warm", "fast"),
        (
            "```python\ndef f():\n    return 1 / 0\n```\n为什么报错？",
            0.0,
            False,
            "chat",
            "safe",
            "serious",
            "deep",
        ),
        (
            "帮我设计一个千万级并发的分布式限流系统，给出完整架构、数据结构和故障降级方案，越详细越好",
            0.0,
            False,
            "chat",
            "safe",
            "serious",
            "max",
        ),
        ("我今天真的好累，什么都不想做", -0.7, False, "chat", "safe", "sad", "low"),
        ("帮我搜索一下明天北京的天气", 0.0, True, "search", "caution", "calm", "low"),
        ("记住我喜欢喝冰美式", 0.0, False, "remember", "safe", "warm", "low"),
        ("你还记得上次我们聊的那个方案吗", 0.0, False, "recall", "safe", "calm", "low"),
        ("帮我截图看看屏幕上的报错", 0.0, False, "observe", "caution", "serious", "deep"),
        ("走到我旁边来", 0.0, False, "move", "caution", "calm", "low"),
        ("帮我把数据库删库跑路", 0.0, False, "chat", "deny", "serious", "deep"),
    ],
)
def test_rules_fallback_is_sensible(
    text, emotion, has_tools, intent, risk, tone, depth
):
    frame = decide_by_rules_frame(
        text,
        emotion_index=emotion,
        has_tools=has_tools,
    )
    assert frame.source == "rules"
    assert frame.attend is True and frame.speak is True
    assert frame.intent == intent
    assert frame.risk == risk
    assert frame.tone == tone
    assert frame.depth == depth
    assert frame.reason.startswith("rules:")
    assert f"intent={intent}" in frame.reason
    assert f"risk={risk}" in frame.reason
    assert f"tone={tone}" in frame.reason


def test_rules_fallback_default_is_low_chat_safe_calm():
    frame = decide_by_rules_frame("今天过得怎么样")
    assert frame.depth == "low"  # normal tier maps to low, not thinking tier
    assert frame.intent == "chat"
    assert frame.risk == "safe"
    assert frame.tone == "calm"
    assert frame.confidence == 0.0


# ── Frame defaults and budget mapping ──────────────────────────────


def test_frame_defaults_are_low_neutral():
    frame = DecisionFrame()
    assert frame.attend is True
    assert frame.speak is True
    assert frame.intent == "chat"
    assert frame.depth == "low"
    assert frame.risk == "safe"
    assert frame.tone == "calm"
    assert frame.source == "rules"
    assert frame.questions_raw is None


def test_frame_rejects_unknown_values():
    with pytest.raises(ValueError):
        DecisionFrame(depth="ultra")
    with pytest.raises(ValueError):
        DecisionFrame(intent="dance")
    with pytest.raises(ValueError):
        DecisionFrame(risk="maybe")
    with pytest.raises(ValueError):
        DecisionFrame(tone="angry")


@pytest.mark.parametrize(
    "depth,thinking,effort,tokens",
    [
        ("fast", False, "low", 384),
        ("low", False, "low", 768),
        ("deep", True, "high", 2048),
        ("max", True, "max", 4096),
    ],
)
def test_frame_to_budget_maps_each_depth(depth, thinking, effort, tokens):
    frame = DecisionFrame(depth=depth, source="laya", confidence=0.9, reason="x")
    budget = frame_to_budget(frame)

    assert budget.tier == depth
    assert budget.thinking is thinking
    assert budget.reasoning_effort == effort
    assert budget.max_tokens == tokens
    assert budget.source == "laya"
    assert budget.confidence == pytest.approx(0.9)
    assert budget.reason.startswith(f"frame:{depth}")


def test_frame_to_budget_tool_floor():
    fast = frame_to_budget(DecisionFrame(depth="fast"), has_tools=True)
    low = frame_to_budget(DecisionFrame(depth="low"), has_tools=True)
    deep = frame_to_budget(DecisionFrame(depth="deep"), has_tools=True)
    max_ = frame_to_budget(DecisionFrame(depth="max"), has_tools=True)

    assert fast.max_tokens == 1024
    assert low.max_tokens == 1024
    assert deep.max_tokens == 2048
    assert max_.max_tokens == 4096


def test_frame_to_budget_low_mood_demotes_max():
    frame = DecisionFrame(depth="max", source="laya", confidence=0.9)
    budget = frame_to_budget(frame, emotion_index=-0.7)

    assert budget.tier == "deep"
    assert budget.thinking is True
    assert budget.reasoning_effort == "high"
    assert budget.max_tokens == 2048
    assert "frame:max->deep" in budget.reason


def test_frame_to_budget_low_mood_never_drops_below_tool_floor():
    frame = DecisionFrame(depth="low")
    budget = frame_to_budget(frame, has_tools=True, emotion_index=-0.7)
    assert budget.max_tokens == 1024
