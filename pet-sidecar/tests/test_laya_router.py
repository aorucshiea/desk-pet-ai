"""Laya budget router tests -- no real sidecar involved.

Pins the gateway contract:
  * all four choices map to the documented thinking/effort/token budgets;
  * messages shorter than 4 characters never touch HTTP;
  * a normalized-text LRU coalesces repeated requests;
  * timeouts, transport errors, non-200s, malformed payloads, invalid
    choices and low confidence all fall back to deterministic rules;
  * tool availability lifts fast/normal token budgets;
  * rules produce sensible tiers for greetings/code/long text/tools;
  * PET_LAYA_ENABLED=0 bypasses the sidecar.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from gateway.decision.laya_router import (
    BudgetDecision,
    clear_cache,
    decide_budget,
    decide_by_rules,
    map_tier,
)

_TIERS = ("fast", "normal", "deep", "max")


@pytest.fixture(autouse=True)
def _clean_router(monkeypatch):
    monkeypatch.delenv("PET_LAYA_URL", raising=False)
    monkeypatch.delenv("PET_LAYA_TIMEOUT", raising=False)
    monkeypatch.delenv("PET_LAYA_ENABLED", raising=False)
    clear_cache()
    yield
    clear_cache()


def _answer(choice: str, confidence: float = 0.9) -> dict:
    """A minimal /v1/systemone response with a valid selected confidence."""
    other = (1.0 - confidence) / 3.0
    probabilities = {tier: other for tier in _TIERS}
    probabilities[choice] = confidence
    return {
        "answers": {
            "thinking": {
                "type": "choice",
                "choice": choice,
                "probabilities": probabilities,
                "confidence": 0.5,  # normalized entropy, deliberately not used
                "answer_confidence": confidence,
            }
        },
        "usage": {"input_tokens": 12, "output_tokens": 0},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "choice,thinking,effort,tokens",
    [
        ("fast", False, "low", 512),
        ("normal", False, "low", 1024),
        ("deep", True, "high", 2048),
        ("max", True, "max", 4096),
    ],
)
async def test_laya_choices_map_to_budgets(choice, thinking, effort, tokens):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json=_answer(choice, 0.9))

    text = f"请认真回答这个问题，编号 {choice}"
    decision = await decide_budget(text, transport=httpx.MockTransport(handler))

    assert isinstance(decision, BudgetDecision)
    assert decision.tier == choice
    assert decision.thinking is thinking
    assert decision.reasoning_effort == effort
    assert decision.max_tokens == tokens
    assert decision.source == "laya"
    assert decision.confidence == pytest.approx(0.9)
    assert decision.reason.startswith(f"laya:{choice}@")
    assert len(bodies) == 1
    assert bodies[0]["model"] == "zh-tier"
    assert bodies[0]["state"] == text
    assert set(bodies[0]["questions"]["thinking"]["criteria"]) == set(_TIERS)


@pytest.mark.parametrize(
    "choice,thinking,effort,tokens",
    [
        ("fast", False, "low", 512),
        ("normal", False, "low", 1024),
        ("deep", True, "high", 2048),
        ("max", True, "max", 4096),
    ],
)
def test_map_tier_is_pure_and_matches_spec(choice, thinking, effort, tokens):
    decision = map_tier(choice)
    assert (decision.tier, decision.thinking) == (choice, thinking)
    assert decision.reasoning_effort == effort
    assert decision.max_tokens == tokens
    assert decision.source == "rules"  # caller labels the origin


def test_map_tier_rejects_unknown_tier():
    with pytest.raises(ValueError):
        map_tier("ultra")


def test_tools_raise_fast_and_normal_token_floor():
    assert map_tier("fast", has_tools=True).max_tokens == 1024
    assert map_tier("normal", has_tools=True).max_tokens == 1024
    assert map_tier("deep", has_tools=True).max_tokens == 2048
    assert map_tier("max", has_tools=True).max_tokens == 4096


@pytest.mark.asyncio
async def test_has_tools_bumps_laya_fast_budget():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_answer("fast", 0.8))

    decision = await decide_budget(
        "帮我看看这个文件",
        has_tools=True,
        transport=httpx.MockTransport(handler),
    )
    assert decision.tier == "fast"
    assert decision.source == "laya"
    assert decision.max_tokens == 1024


def test_low_mood_demotes_max_and_caps_tokens():
    decision = map_tier("max", emotion_index=-0.7)
    assert decision.tier == "deep"
    assert decision.thinking is True
    assert decision.reasoning_effort == "high"
    assert decision.max_tokens == 2048


def test_low_mood_applies_to_rules_max_hint():
    text = "请给我一个完整的系统设计方案，覆盖所有模块和异常路径。"
    normal = decide_by_rules(text)
    low = decide_by_rules(text, emotion_index=-0.7)

    assert normal.tier == "max"
    assert normal.max_tokens == 4096
    assert low.tier == "deep"
    assert low.max_tokens == 2048


@pytest.mark.asyncio
async def test_short_text_never_calls_sidecar():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_answer("max", 0.99))

    transport = httpx.MockTransport(handler)
    for text in ("", "   ", "嗯", "你好", "ok"):
        decision = await decide_budget(text, transport=transport)
        assert decision.source == "short"
        assert decision.tier == "fast"
        assert decision.thinking is False
        assert decision.max_tokens == 512
        assert decision.reason == "short_text"

    assert calls == 0


@pytest.mark.asyncio
async def test_cache_hit_sends_one_request():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json=_answer("normal", 0.8))

    transport = httpx.MockTransport(handler)
    text = "今天天气怎么样，适合出去散步吗？"

    first = await decide_budget(text, transport=transport)
    second = await decide_budget(f"  {text}  ", transport=transport)

    assert first.source == "laya"
    assert second.source == "cache"
    assert (second.tier, second.thinking) == (first.tier, first.thinking)
    assert second.max_tokens == first.max_tokens
    assert second.confidence == first.confidence
    assert len(bodies) == 1


@pytest.mark.asyncio
async def test_low_confidence_falls_back_to_rules():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_answer("max", 0.2))

    text = "```python\ndef f():\n    return 1 / 0\n```\n为什么报错？"
    decision = await decide_budget(text, transport=httpx.MockTransport(handler))

    assert decision.source == "rules"
    assert decision.tier == "deep"
    assert decision.thinking is True
    assert "low_confidence" in decision.reason


@pytest.mark.asyncio
async def test_timeout_falls_back_to_rules(monkeypatch):
    monkeypatch.setenv("PET_LAYA_TIMEOUT", "0.02")

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=_answer("max", 0.99))

    decision = await decide_budget(
        "早上好呀",
        transport=httpx.MockTransport(handler),
    )

    assert decision.source == "rules"
    assert decision.tier == "fast"
    assert decision.reason.startswith("laya_timeout|")


@pytest.mark.asyncio
async def test_transport_error_falls_back_to_rules():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sidecar down", request=request)

    decision = await decide_budget(
        "早上好呀",
        transport=httpx.MockTransport(handler),
    )

    assert decision.source == "rules"
    assert decision.tier == "fast"
    assert decision.reason.startswith("laya_error:ConnectError|")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,payload,reason_part",
    [
        (503, {"error": "busy"}, "laya_http_503"),
        (200, b"{not json", "laya_invalid_json"),
        (
            200,
            {"answers": {"thinking": {"choice": "ultra", "answer_confidence": 0.99}}},
            "laya_invalid_choice",
        ),
        (
            200,
            {"answers": {"thinking": {"choice": "deep"}}},
            "laya_invalid_confidence",
        ),
        (
            200,
            {"answers": {"thinking": {"choice": "deep", "answer_confidence": "high"}}},
            "laya_invalid_confidence",
        ),
        (200, {"answers": []}, "laya_invalid_payload"),
        (
            200,
            {"answers": {"thinking": {"choice": 3, "answer_confidence": 0.9}}},
            "laya_invalid_choice",
        ),
    ],
)
async def test_bad_sidecar_responses_fall_back_to_rules(status, payload, reason_part):
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(payload, bytes):
            return httpx.Response(status, content=payload)
        return httpx.Response(status, json=payload)

    decision = await decide_budget(
        "早上好呀",
        transport=httpx.MockTransport(handler),
    )

    assert decision.source == "rules"
    assert decision.tier == "fast"
    assert reason_part in decision.reason


@pytest.mark.asyncio
async def test_probabilities_used_when_answer_confidence_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "answers": {
                "thinking": {
                    "choice": "deep",
                    "probabilities": {"deep": 0.8, "fast": 0.1, "normal": 0.05, "max": 0.05},
                }
            }
        }
        return httpx.Response(200, json=payload)

    decision = await decide_budget(
        "这段逻辑为什么不对",
        transport=httpx.MockTransport(handler),
    )

    assert decision.source == "laya"
    assert decision.tier == "deep"
    assert decision.confidence == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_disabled_bypasses_sidecar(monkeypatch):
    monkeypatch.setenv("PET_LAYA_ENABLED", "0")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("disabled router must not call Laya")

    decision = await decide_budget(
        "帮我分析一下这段 Python 报错\n```\nKeyError\n```",
        transport=httpx.MockTransport(handler),
    )

    assert decision.source == "rules"
    assert decision.tier == "deep"
    assert decision.reason.startswith("laya_disabled|")


@pytest.mark.asyncio
async def test_request_contract_uses_default_url():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_answer("normal", 0.8))

    text = "今天天气怎么样，适合散步吗？"
    decision = await decide_budget(text, transport=httpx.MockTransport(handler))

    assert decision.source == "laya"
    assert seen["url"] == "http://127.0.0.1:8765/v1/systemone"
    assert seen["body"]["model"] == "zh-tier"
    assert seen["body"]["state"] == text
    question = seen["body"]["questions"]["thinking"]
    assert question["type"] == "choice"
    assert question["instructions"]
    assert set(question["criteria"]) == set(_TIERS)


@pytest.mark.asyncio
async def test_laya_url_env_override(monkeypatch):
    monkeypatch.setenv("PET_LAYA_URL", "http://127.0.0.1:18765/")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_answer("fast", 0.8))

    await decide_budget("帮我看看这个", transport=httpx.MockTransport(handler))
    assert seen["url"] == "http://127.0.0.1:18765/v1/systemone"


def test_rules_greeting_code_long_and_tools():
    greeting = decide_by_rules("早上好呀")
    assert greeting.tier == "fast"
    assert greeting.source == "rules"
    assert greeting.max_tokens == 512
    assert greeting.confidence == 0.0

    code = "```python\ndef f():\n    return 1 / 0\n```\n为什么报错？"
    code_decision = decide_by_rules(code)
    assert code_decision.tier == "deep"
    assert code_decision.thinking is True
    assert code_decision.reasoning_effort == "high"
    assert code_decision.max_tokens == 2048
    assert "code" in code_decision.reason

    long_text = "请把这段经历整理成一篇完整的复盘。" + "细节" * 120
    long_decision = decide_by_rules(long_text)
    assert long_decision.tier == "deep"
    assert "long" in long_decision.reason

    tool_call = decide_by_rules("帮我查一下明天的天气", has_tools=True)
    assert tool_call.tier == "normal"
    assert tool_call.max_tokens == 1024
    assert "tools" in tool_call.reason

    greeting_with_tools = decide_by_rules("早上好呀", has_tools=True)
    assert greeting_with_tools.tier == "fast"
    assert greeting_with_tools.max_tokens == 1024


def test_short_rules_decision_stays_fast():
    decision = decide_by_rules("嗯")
    assert decision.tier == "fast"
    assert decision.source == "rules"


def test_ascii_greeting_prefix_does_not_eat_real_words():
    # "hi" is a prefix of "high"/"history"; only a word boundary counts.
    assert decide_by_rules("high").tier != "fast"
    assert decide_by_rules("history").tier != "fast"
    assert decide_by_rules("hi there").tier == "fast"


@pytest.mark.asyncio
async def test_custom_client_is_not_closed_by_router():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_answer("normal", 0.8))
        ),
    )
    try:
        decision = await decide_budget("今天过得还不错吧", client=client)
        assert decision.source == "laya"
        assert not client.is_closed
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_floor_does_not_promote_a_simple_weather_question():
    decision = await decide_budget(
        "今天天气怎么样？",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_answer("normal", 0.57))
        ),
    )
    assert decision.tier == "normal"
    assert decision.source == "laya"
    assert "floor" not in decision.reason


@pytest.mark.asyncio
async def test_floor_promotes_confident_fast_on_sql_to_deep():
    decision = await decide_budget(
        "帮我分析下这条 SQL 为什么这么慢，索引该怎么建",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_answer("fast", 0.79))
        ),
    )
    assert decision.tier == "deep"
    assert decision.thinking is True
    assert decision.max_tokens == 2048
    assert "floor:code" in decision.reason


@pytest.mark.asyncio
async def test_architecture_low_confidence_falls_back_to_max():
    decision = await decide_budget(
        "帮我设计一个千万级并发的分布式限流系统，给出完整架构、数据结构和故障降级方案，越详细越好",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_answer("fast", 0.38))
        ),
    )
    assert decision.tier == "max"
    assert decision.source == "rules"
    assert decision.max_tokens == 4096
    assert "max_hint" in decision.reason


def test_simple_question_rules_do_not_use_generic_meta_hint():
    assert decide_by_rules("今天天气怎么样？").tier == "normal"
    assert decide_by_rules("你叫什么名字？").tier == "normal"
    assert decide_by_rules("这段代码为什么报错？").tier == "deep"
