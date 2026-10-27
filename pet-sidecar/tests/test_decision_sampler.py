"""Decision sampler tests -- no real Laya sidecar involved.

Pins the contract:
  * input validation (option count / ids / labels / weights);
  * prior/laya combination, ranking and seeded reproducibility;
  * top-3 shortlist sampling: the posterior tail can never win a roll;
  * one POST with the dynamic criteria and compact state;
  * timeouts, 500s, invalid choices and malformed JSON degrade to prior;
  * one-hot parsing when probabilities are absent;
  * JSONL roll + override logging and the recent-roll LRU;
  * PET_LAYA_ENABLED=0 never touches HTTP.
"""

from __future__ import annotations

import asyncio
import json
import random

import httpx
import pytest

from gateway.decision.laya_router import LAYA_MODEL
from gateway.decision.sampler import (
    MAX_OPTIONS,
    clear_recent,
    record_override,
    roll_decision,
)

_OPTIONS = [
    {"id": "a", "label": "甲", "weight": 1.0},
    {"id": "b", "label": "乙", "weight": 1.0},
    {"id": "c", "label": "丙", "weight": 1.0},
]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for name in (
        "PET_LAYA_ENABLED",
        "PET_LAYA_TIMEOUT",
        "PET_LAYA_ROLL_TIMEOUT",
        "PET_LAYA_URL",
        "PET_DECISION_LOG",
    ):
        monkeypatch.delenv(name, raising=False)
    # Never append test rolls to the real ~/.pet log.
    monkeypatch.setenv("PET_DECISION_LOG", str(tmp_path / "default-rolls.jsonl"))
    clear_recent()
    yield
    clear_recent()


def _payload(roll: dict) -> dict:
    return {"answers": {"roll": roll}}


def _transport(roll: dict, seen: list | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json=_payload(roll))

    return httpx.MockTransport(handler)


def _forbidden(request: httpx.Request) -> httpx.Response:
    raise AssertionError("invalid input must not reach Laya")


@pytest.mark.asyncio
async def test_rejects_invalid_input():
    good = [{"id": "a", "label": "甲"}, {"id": "b", "label": "乙"}]
    transport = httpx.MockTransport(_forbidden)

    async def expect(question: str = "做什么？", options=good):
        with pytest.raises(ValueError):
            await roll_decision(
                question,
                options,
                seed=1,
                transport=transport,
            )

    await expect(options=[good[0]])
    await expect(
        options=good
        + [{"id": f"o{index}", "label": str(index)} for index in range(MAX_OPTIONS)]
    )
    await expect(options=[good[0], {"id": "a", "label": "重复"}])
    await expect(options=[{"id": "a b", "label": "空格"}, good[1]])
    await expect(options=[{"id": "a!", "label": "符号"}, good[1]])
    await expect(options=[{"id": "a\n", "label": "换行"}, good[1]])
    await expect(options=[{"id": "x" * 25, "label": "太长"}, good[1]])
    await expect(question="   ")
    await expect(options=[{"id": "a", "label": "   "}, good[1]])
    await expect(options=[{"id": "a", "label": "甲", "weight": True}, good[1]])
    await expect(
        options=[{"id": "a", "label": "甲", "weight": float("nan")}, good[1]]
    )


@pytest.mark.asyncio
async def test_equal_weights_rank_and_seeded_sample(tmp_path):
    probabilities = {"a": 0.7, "b": 0.2, "c": 0.1}
    transport = _transport({"choice": "a", "probabilities": probabilities})
    path = tmp_path / "rolls.jsonl"

    first = await roll_decision(
        "接下来做什么？",
        _OPTIONS,
        user_text="在写代码，有点卡住了",
        seed=1,
        transport=transport,
        log_path=path,
    )
    second = await roll_decision(
        "接下来做什么？",
        _OPTIONS,
        user_text="在写代码，有点卡住了",
        seed=1,
        transport=transport,
        log_path=path,
    )

    assert first["source"] == "laya+prior"
    posterior = first["posterior"]
    assert posterior["a"] > posterior["b"] > posterior["c"]
    assert sum(posterior.values()) == pytest.approx(1.0)
    # Three options fit in the shortlist whole, ordered by posterior.
    assert first["shortlist"] == ["a", "b", "c"]
    assert f"|shortlist=a,b,c" in first["reason"]

    # Seed 1 and seed 0 were verified to pick different options with this
    # posterior, so the seed really drives the sample.
    assert first["choice"] == "a"
    assert second["choice"] == first["choice"]
    assert second["posterior"] == first["posterior"]

    # Sampling happens over the SHORTLIST weights, not all options.
    expected = random.Random(1).choices(
        first["shortlist"],
        weights=[posterior[option_id] for option_id in first["shortlist"]],
        k=1,
    )[0]
    assert first["choice"] == expected

    other = await roll_decision(
        "接下来做什么？",
        _OPTIONS,
        user_text="在写代码，有点卡住了",
        seed=0,
        transport=transport,
        log_path=path,
    )
    assert other["choice"] == "c"
    assert other["posterior"] == first["posterior"]
    # ``confidence`` is the posterior over ALL options, never renormalized
    # to the shortlist — it must stay a calibrated signal.
    assert other["confidence"] == pytest.approx(posterior["c"])
    assert sum(posterior.values()) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_strong_prior_dominates_laya(tmp_path):
    options = [
        {"id": "a", "label": "继续", "weight": 0.95},
        {"id": "b", "label": "放弃", "weight": 0.05},
    ]
    transport = _transport({"choice": "b", "probabilities": {"a": 0.1, "b": 0.9}})

    result = await roll_decision(
        "要继续吗？",
        options,
        seed=1,
        transport=transport,
        log_path=tmp_path / "rolls.jsonl",
    )

    assert result["source"] == "laya+prior"
    assert result["laya"]["choice"] == "b"
    assert result["prior"]["a"] == pytest.approx(0.95)
    # With exponents 0.85/0.6 a strongly-held prior still wins even when Laya
    # is reversed: prior .95/.05 vs Laya .1/.9 lands around 77/23 (and
    # .9/.1 vs .1/.9 lands around 63/37), so the prior stays authoritative.
    assert result["posterior"]["a"] > result["posterior"]["b"]
    assert result["posterior"]["a"] == pytest.approx(0.766, abs=0.02)

    # The documented balance point: prior .9/.1 with Laya fully reversed.
    mild = await roll_decision(
        "要继续吗？",
        [
            {"id": "a", "label": "继续", "weight": 0.9},
            {"id": "b", "label": "放弃", "weight": 0.1},
        ],
        seed=1,
        transport=transport,
        log_path=tmp_path / "rolls.jsonl",
    )
    assert mild["posterior"]["a"] == pytest.approx(0.634, abs=0.02)


@pytest.mark.asyncio
async def test_posterior_equal_priors_laya_leads_but_stays_calibrated(tmp_path):
    """Live case: equal gut weights + Laya {observe .465 ... move .029}.

    At 0.7/0.3 this posterior was a flat 32/29/25/14; 0.85/0.6 must give
    Laya a clearer lead while the strong-prior case above still favours the
    prior.
    """
    options = [
        {"id": "observe", "label": "看屏幕", "weight": 1.0},
        {"id": "chat", "label": "聊天", "weight": 1.0},
        {"id": "recall", "label": "回忆", "weight": 1.0},
        {"id": "move", "label": "移动", "weight": 1.0},
    ]
    transport = _transport({
        "choice": "observe",
        "probabilities": {
            "observe": 0.465, "chat": 0.311, "recall": 0.194, "move": 0.029,
        },
    })

    result = await roll_decision(
        "接下来做什么？",
        options,
        seed=2,
        transport=transport,
        log_path=tmp_path / "rolls.jsonl",
    )

    posterior = result["posterior"]
    assert posterior["observe"] > 0.35
    assert posterior["observe"] > posterior["chat"] > posterior["recall"]
    assert result["shortlist"] == ["observe", "chat", "recall"]


@pytest.mark.asyncio
async def test_tail_option_never_wins_a_roll(tmp_path):
    """The 2.9% tail must stay out of the shortlist and never be sampled."""
    options = [
        {"id": "observe", "label": "看屏幕", "weight": 1.0},
        {"id": "chat", "label": "聊天", "weight": 1.0},
        {"id": "recall", "label": "回忆", "weight": 1.0},
        {"id": "move", "label": "移动", "weight": 1.0},
    ]
    transport = _transport({
        "choice": "observe",
        "probabilities": {
            "observe": 0.465, "chat": 0.311, "recall": 0.194, "move": 0.029,
        },
    })

    seen = set()
    for seed in range(51):
        result = await roll_decision(
            "接下来做什么？",
            options,
            seed=seed,
            transport=transport,
            log_path=tmp_path / "rolls.jsonl",
        )
        assert "move" not in result["shortlist"]
        assert result["choice"] in result["shortlist"]
        assert result["choice"] != "move", f"tail option won at seed={seed}"
        assert result["reason"].endswith("|shortlist=observe,chat,recall")
        seen.add(result["choice"])

    # Whimsy survives the shortlist: more than one option can still win.
    assert seen == {"observe", "chat", "recall"}


@pytest.mark.asyncio
async def test_request_contract_and_laya_probabilities(tmp_path):
    seen: list[dict] = []
    options = [
        {"id": "observe", "label": "看屏幕/观察", "weight": 0.8},
        {"id": "wait", "label": "先等等", "weight": 0.2},
    ]
    transport = _transport(
        {"choice": "observe", "probabilities": {"observe": 0.75, "wait": 0.25}},
        seen=seen,
    )

    result = await roll_decision(
        "现在该做什么？",
        options,
        context="桌面上开着编辑器和终端",
        user_text="帮我整理一下思路",
        seed=3,
        transport=transport,
        log_path=tmp_path / "rolls.jsonl",
    )

    assert len(seen) == 1
    body = seen[0]
    assert body["model"] == LAYA_MODEL
    assert list(body["questions"]) == ["roll"]

    question = body["questions"]["roll"]
    assert question["type"] == "choice"
    assert question["instructions"].endswith("现在该做什么？")
    assert question["criteria"] == {
        "observe": "看屏幕/观察",
        "wait": "先等等",
    }

    state = body["state"]
    assert "你是桌宠的潜意识直觉层" in state
    assert "帮我整理一下思路" in state
    assert "桌面上开着编辑器和终端" in state
    assert len(state) <= 900

    assert result["source"] == "laya+prior"
    assert result["laya"]["choice"] == "observe"
    assert result["laya"]["probabilities"]["observe"] == pytest.approx(0.75)
    assert result["laya"]["probabilities"]["wait"] == pytest.approx(0.25)
    assert result["laya"]["source"] == "laya"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["timeout", "http_500", "invalid_choice", "bad_json"],
)
async def test_laya_failures_degrade_to_prior(kind, tmp_path):
    timeout = None
    if kind == "timeout":
        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.2)
            return httpx.Response(
                200,
                json=_payload(
                    {"choice": "a", "probabilities": {"a": 0.9, "b": 0.1}}
                ),
            )

        transport = httpx.MockTransport(handler)
        timeout = 0.03
    elif kind == "http_500":
        transport = httpx.MockTransport(
            lambda request: httpx.Response(500, json={"error": "busy"})
        )
    elif kind == "invalid_choice":
        transport = _transport(
            {"choice": "zzz", "probabilities": {"a": 0.9, "b": 0.1}}
        )
    else:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"{not json")
        )

    result = await roll_decision(
        "接下来做什么？",
        _OPTIONS[:2],
        seed=1,
        transport=transport,
        timeout=timeout,
        log_path=tmp_path / "rolls.jsonl",
    )

    assert result["ok"] is True
    assert result["source"] == "prior"
    assert result["choice"] in {"a", "b"}
    assert result["posterior"] == result["prior"]
    assert result["laya"]["source"] == "error"
    assert result["laya"]["error"]
    assert result["reason"].startswith(result["laya"]["error"])


@pytest.mark.asyncio
async def test_missing_probabilities_uses_one_hot(tmp_path):
    transport = _transport({"choice": "b", "answer_confidence": 0.8})

    result = await roll_decision(
        "接下来做什么？",
        _OPTIONS[:2],
        seed=1,
        transport=transport,
        log_path=tmp_path / "rolls.jsonl",
    )

    assert result["source"] == "laya+prior"
    assert result["laya"]["choice"] == "b"
    assert result["laya"]["confidence"] == pytest.approx(0.8)
    assert result["laya"]["probabilities"]["b"] == pytest.approx(0.8)
    assert result["laya"]["probabilities"]["a"] == pytest.approx(0.2)
    assert sum(result["laya"]["probabilities"].values()) == pytest.approx(1.0)

    no_confidence = await roll_decision(
        "接下来做什么？",
        _OPTIONS[:2],
        seed=1,
        transport=_transport({"choice": "b"}),
        log_path=tmp_path / "rolls.jsonl",
    )
    assert no_confidence["laya"]["confidence"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_jsonl_roll_and_override_logging(tmp_path):
    path = tmp_path / "nested" / "rolls.jsonl"
    result = await roll_decision(
        "接下来做什么？",
        _OPTIONS[:2],
        user_text="在写代码",
        context="桌面",
        seed=1,
        transport=_transport(
            {"choice": "a", "probabilities": {"a": 0.7, "b": 0.3}}
        ),
        log_path=path,
    )

    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    roll = records[0]
    assert roll["type"] == "roll"
    assert roll["roll_id"] == result["roll_id"]
    assert roll["question"] == "接下来做什么？"
    assert roll["choice"] == result["choice"]
    assert roll["shortlist"] == result["shortlist"]
    assert roll["seed"] == result["seed"]
    assert roll["user_text"] == "在写代码"
    assert roll["context"] == "桌面"
    assert roll["laya"]["source"] == "laya"
    assert "error" not in roll["laya"]
    assert roll["ts"].endswith("Z")

    override = record_override(
        result["roll_id"], "b", "更符合当下", log_path=path
    )
    assert override["ok"] is True
    assert override["known"] is True
    assert override["roll"]["roll_id"] == result["roll_id"]

    unknown = record_override(
        "roll_0000000000000000", "a", "事后想想", log_path=path
    )
    assert unknown["ok"] is True
    assert unknown["known"] is False
    assert unknown["roll"] is None

    flagged = record_override(
        result["roll_id"], "zzz", "不在选项里", log_path=path
    )
    assert flagged["known"] is True

    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 4
    assert records[1]["type"] == "override"
    assert records[1]["known"] is True
    assert records[1]["roll_id"] == result["roll_id"]
    assert records[1]["roll"]["roll_id"] == result["roll_id"]
    assert records[2]["known"] is False
    assert records[2]["roll"] is None
    assert records[3]["choice_not_in_options"] is True

    with pytest.raises(ValueError):
        record_override("", "a", "x", log_path=path)
    with pytest.raises(ValueError):
        record_override(result["roll_id"], "", "x", log_path=path)
    with pytest.raises(ValueError):
        record_override(result["roll_id"], "a", "  ", log_path=path)


@pytest.mark.asyncio
async def test_disabled_laya_never_calls_http(monkeypatch, tmp_path):
    monkeypatch.setenv("PET_LAYA_ENABLED", "0")

    result = await roll_decision(
        "接下来做什么？",
        _OPTIONS,
        seed=1,
        transport=httpx.MockTransport(_forbidden),
        log_path=tmp_path / "rolls.jsonl",
    )

    assert result["ok"] is True
    assert result["source"] == "prior"
    assert result["posterior"] == result["prior"]
    assert result["laya"]["source"] == "error"
    assert "disabled" in result["laya"]["error"]
    assert result["reason"].startswith("laya_disabled|")
