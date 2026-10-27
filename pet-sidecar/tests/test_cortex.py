"""D cortex tests: DSTATE state, one-call Laya decisions, and persistence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os

import httpx
import pytest

from gateway.decision.cortex import (
    DEFAULT_ACTION_OPTIONS,
    DEFAULT_REACTION_OPTIONS,
    DEFAULT_STRENGTH_OPTIONS,
    REACTION_MAX_TOKENS,
    DStateStore,
    active_decision,
    build_laya_state,
    clear_recent,
    decision_to_prompt,
    estimate_tokens,
    parse_dstate_block,
    passive_decision,
    strip_dstate_blocks,
    truncate_summary,
    update_from_round_text,
)
from gateway.decision.laya_router import LAYA_MODEL


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for name in ("PET_LAYA_ENABLED", "PET_LAYA_TIMEOUT", "PET_LAYA_ROLL_TIMEOUT", "PET_LAYA_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PET_DECISION_LOG", str(tmp_path / "decisions.jsonl"))
    clear_recent()
    yield
    clear_recent()


def _block(summary="我刚刚看了桌面。", **sections) -> str:
    return "<<<DSTATE>>>" + json.dumps(
        {"summary": summary, **sections}, ensure_ascii=False
    ) + "<<<DSTATEEND>>>"


def _choice(choice: str, ids: list[str]) -> dict:
    return {
        "type": "choice",
        "choice": choice,
        "probabilities": {option_id: float(option_id == choice) for option_id in ids},
        "answer_confidence": 1.0,
    }


def _laya_payload(
    strength="low", reaction="large", action="walk", actions=None
) -> dict:
    action_ids = actions or [option["id"] for option in DEFAULT_ACTION_OPTIONS]
    return {
        "answers": {
            "thinking": _choice(
                strength, [option["id"] for option in DEFAULT_STRENGTH_OPTIONS]
            ),
            "reaction": _choice(
                reaction, [option["id"] for option in DEFAULT_REACTION_OPTIONS]
            ),
            "action": _choice(action, action_ids),
        }
    }


def _transport(payload, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content.decode("utf-8")))
        if isinstance(payload, httpx.Response):
            return payload
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def test_dstate_parse_update_uses_last_block_and_strips_all():
    store = DStateStore()
    text = "之前" + _block("旧") + "中间" + _block("新") + "之后"

    parsed, cleaned = parse_dstate_block(text)
    assert parsed["summary"] == "新"
    assert cleaned == "之前中间之后"

    changed, cleaned = update_from_round_text(store, text)
    assert changed is True
    assert store.summary == "新"
    assert cleaned == "之前中间之后"
    assert store.updated_at.endswith("Z")


def test_invalid_json_and_missing_blocks_never_leak_markers():
    store = DStateStore()
    original = "你好"
    assert update_from_round_text(store, original) == (False, original)

    malformed = "可见<<<DSTATE>>>{bad<<<DSTATEEND>>>尾部"
    changed, cleaned = update_from_round_text(store, malformed)
    assert changed is False
    assert cleaned == "可见尾部"
    assert parse_dstate_block(malformed)[0] is None


def test_unterminated_trailing_block_is_stripped():
    text = "给用户看" + _block("完整") + "<<<DSTATE>>>{\"summary\":\"未完成"
    assert strip_dstate_blocks(text) == "给用户看"
    parsed, cleaned = parse_dstate_block(text)
    assert parsed["summary"] == "完整"
    assert cleaned == "给用户看"


def test_empty_or_invalid_sections_keep_previous_and_weights_are_clamped():
    store = DStateStore()
    previous = store.action_options
    changed, _ = update_from_round_text(
        store,
        _block(
            "总结",
            action_options=[],
            strength_options=[{"id": "deep", "label": "深", "weight": 1}],
            reaction_options=[
                {"id": "small", "label": "小", "weight": -2},
                {"id": "large", "label": "大", "weight": 4},
            ],
        ),
    )
    assert changed is True
    assert store.action_options == previous
    assert store.action_options is not previous
    assert store.strength_options == DEFAULT_STRENGTH_OPTIONS
    assert store.reaction_options == [
        {"id": "small", "label": "小", "weight": 0.0},
        {"id": "large", "label": "大", "weight": 1.0},
    ]


def test_action_options_are_capped_at_eight():
    store = DStateStore()
    options = [
        {"id": f"a{index}", "label": str(index), "weight": 1}
        for index in range(10)
    ]
    store.update({"summary": "x", "action_options": options})
    assert len(store.action_options) == 8
    assert store.action_options[-1]["id"] == "a7"


def test_token_estimate_and_truncate_ascii_cjk_and_unicode():
    assert estimate_tokens("hello world") == 2
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("猫!") == 2  # one CJK + punctuation .25, rounded up
    assert estimate_tokens("😀") == 1
    assert truncate_summary("hello world", 1) == "hello"
    assert truncate_summary("你好世界", 2) == "你好"
    assert truncate_summary("😀😀", 0.25) == "😀"
    assert truncate_summary("abc", 0) == ""


def test_update_truncates_summary_to_1024_estimated_tokens():
    store = DStateStore()
    changed, _ = update_from_round_text(store, _block("猫" * 1100))
    assert changed is True
    assert len(store.summary) == 1024
    assert estimate_tokens(store.summary) == 1024


def test_build_laya_state_contains_summary_and_current_state():
    store = DStateStore()
    state = build_laya_state(
        store,
        {"user_text": "陪我聊聊", "is_proactive": True, "mood_index": 0.2},
    )
    assert "（还没有生命总结）" in state
    assert "陪我聊聊" in state
    assert "is_proactive：true" in state

    store.summary = "我刚刚散步回来"
    state = build_laya_state(store, {"user_text": "x" * 250, "idle_sec": 12})
    assert "我刚刚散步回来" in state
    assert "x" * 201 not in state
    assert "idle_sec：12" in state


@pytest.mark.asyncio
async def test_passive_decision_one_request_and_complete_mapping():
    store = DStateStore()
    store.summary = "我在桌边观察用户"
    seen = []
    transport = _transport(_laya_payload(), seen)

    decision = await passive_decision(
        store,
        {"user_text": "过来", "is_proactive": False, "last_action": "quiet"},
        transport=transport,
    )

    assert len(seen) == 1
    body = seen[0]
    assert body["model"] == LAYA_MODEL
    assert "我在桌边观察用户" in body["state"]
    assert "过来" in body["state"]
    assert list(body["questions"]) == ["thinking", "reaction", "action"]
    assert body["questions"]["thinking"]["criteria"] == {
        option["id"]: option["label"] for option in DEFAULT_STRENGTH_OPTIONS
    }
    assert body["questions"]["reaction"]["criteria"] == {
        option["id"]: option["label"] for option in DEFAULT_REACTION_OPTIONS
    }
    assert body["questions"]["action"]["criteria"] == {
        option["id"]: option["label"] for option in DEFAULT_ACTION_OPTIONS
    }

    assert decision["decision_id"].startswith("d_")
    assert decision["source"] == "laya+prior"
    assert decision["strength"] == "low"
    assert decision["thinking"] is True
    assert decision["reasoning_effort"] == "low"
    assert decision["reaction"] == "large"
    assert decision["max_tokens"] == REACTION_MAX_TOKENS["large"] == 4096
    assert decision["action"] == "walk"
    assert decision["action_label"] == "走"
    assert decision["summary_used"] is True
    assert 0 <= decision["confidence"] <= 1
    assert store.last_decision == decision

    for qid in ("thinking", "reaction", "action"):
        question = decision["questions"][qid]
        assert question["choice"] in question["posterior"]
        assert question["shortlist"]
        assert set(question["posterior"]) == set(question["prior"])
        assert question["laya_choice"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["http_500", "timeout", "invalid_choice"])
async def test_laya_failures_use_priors_without_raising(kind):
    if kind == "http_500":
        transport = httpx.MockTransport(
            lambda request: httpx.Response(500, json={"error": "busy"})
        )
        timeout = None
    elif kind == "invalid_choice":
        payload = _laya_payload()
        payload["answers"]["action"]["choice"] = "not_an_option"
        transport = _transport(payload)
        timeout = None
    else:
        async def slow(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.1)
            return httpx.Response(200, json=_laya_payload())

        transport = httpx.MockTransport(slow)
        timeout = 0.01

    decision = await passive_decision(
        DStateStore(), transport=transport, timeout=timeout
    )
    assert decision["source"] == "prior"
    assert decision["strength"] in {"no_thinking", "low"}
    assert decision["reaction"] in REACTION_MAX_TOKENS
    assert decision["action"] in {option["id"] for option in DEFAULT_ACTION_OPTIONS}
    for question in decision["questions"].values():
        assert question["posterior"] == question["prior"]
        assert question["laya_choice"] is None
    assert "laya_" in decision["reason"]


@pytest.mark.asyncio
async def test_active_decision_custom_actions_context_and_no_store_mutation():
    store = DStateStore()
    store.summary = "保持现状"
    before = store.to_dict()
    custom = [
        {"id": "dance", "label": "跳舞", "weight": 0.7},
        {"id": "sit", "label": "坐下", "weight": 0.3},
    ]
    seen = []
    payload = _laya_payload(
        strength="no_thinking", reaction="small", action="dance",
        actions=["dance", "sit"],
    )

    decision = await active_decision(
        store,
        action_options=custom,
        context="用户明确邀请我跳舞",
        transport=_transport(payload, seen),
    )

    assert decision["action"] == "dance"
    assert decision["action_label"] == "跳舞"
    assert decision["thinking"] is False
    assert decision["reasoning_effort"] is None
    assert decision["max_tokens"] == 2048
    assert seen[0]["questions"]["action"]["criteria"] == {
        "dance": "跳舞", "sit": "坐下"
    }
    assert "用户明确邀请我跳舞" in seen[0]["state"]
    assert store.to_dict() == before


@pytest.mark.asyncio
async def test_disabled_laya_never_calls_http(monkeypatch):
    monkeypatch.setenv("PET_LAYA_ENABLED", "0")

    def forbidden(request):
        raise AssertionError("disabled Laya must not receive HTTP")

    decision = await passive_decision(
        DStateStore(), transport=httpx.MockTransport(forbidden)
    )
    assert decision["source"] == "prior"
    assert decision["reason"].startswith("laya_disabled")


def test_store_save_load_roundtrip_and_round_update_persists(tmp_path):
    path = tmp_path / "themes" / "cat" / "d-state.json"
    store = DStateStore(path)
    changed, cleaned = update_from_round_text(
        store,
        "回答" + _block(
            "我记住了这次对话",
            action_options=[{"id": "look", "label": "看", "weight": 1}],
        ),
    )
    assert changed is True
    assert cleaned == "回答"
    assert path.exists()

    loaded = DStateStore(path)
    assert loaded.summary == "我记住了这次对话"
    assert loaded.action_options == [{"id": "look", "label": "看", "weight": 1.0}]
    assert loaded.strength_options == DEFAULT_STRENGTH_OPTIONS
    assert loaded.updated_at == store.updated_at

    loaded.last_decision = {"decision_id": "d_test"}
    loaded.save()
    assert DStateStore(path).last_decision == {"decision_id": "d_test"}


def test_corrupt_store_is_tolerated(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("not json", encoding="utf-8")
    store = DStateStore(path)
    assert store.summary == ""
    assert store.action_options == DEFAULT_ACTION_OPTIONS


def test_decision_to_prompt_has_all_fields_and_slow_tool_hint():
    prompt = decision_to_prompt(
        {
            "strength": "no_thinking",
            "reaction": "medium",
            "max_tokens": 3072,
            "action": "look",
            "action_label": "看",
            "summary": "绝不能出现在提示里",
        }
    )
    assert "【这一轮你的底层直觉】" in prompt
    assert "思考强度：不思考" in prompt
    assert "持续反应：中（max_tokens=3072）" in prompt
    assert "默认动作：看" in prompt
    assert "pet_enter_slow" in prompt
    assert "用户直接说话必须回应" in prompt
    assert "绝不能出现在提示里" not in prompt


@pytest.mark.asyncio
async def test_decision_log_hashes_summary_instead_of_logging_it(tmp_path):
    path = tmp_path / "cortex.jsonl"
    os.environ["PET_DECISION_LOG"] = str(path)
    store = DStateStore()
    store.summary = "私密生命总结"
    decision = await passive_decision(store, transport=_transport(_laya_payload()))
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])

    assert record["type"] == "cortex_decision"
    assert record["decision_id"] == decision["decision_id"]
    assert record["summary_sha256"] == hashlib.sha256(
        store.summary.encode("utf-8")
    ).hexdigest()
    assert "私密生命总结" not in json.dumps(record, ensure_ascii=False)
