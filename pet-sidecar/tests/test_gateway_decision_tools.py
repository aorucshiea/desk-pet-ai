"""Gateway wiring for the decision tools (pet_decision_roll / pet_override_decision).

Pins the contract the chat loop depends on:
  * the roll handler passes question/options/context/seed through and reads
    the turn-scoped user text from the ContextVar;
  * the roll is JSON in content + a compact summary, and reports telemetry;
  * invalid args / sampler ValueErrors come back as is_error, never raise;
  * a turn may roll at most 3 times, the 4th is refused without a roll;
  * the override handler logs the veto and never changes the turn;
  * both names stay in CORE_TOOL_NAMES, and native + text ``[MCP:builtin/…]``
    calls both route to the builtin server.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from gateway import server as server_mod
from gateway import telemetry
from gateway.mcp.mcp_manager import MCPManager
from gateway.providers.base import parse_mcp_calls

OPTIONS = [
    {"id": "observe", "label": "看屏幕", "weight": 0.8},
    {"id": "chat", "label": "聊天", "weight": 0.3},
    {"id": "recall", "label": "回忆", "weight": 0.2},
    {"id": "move", "label": "移动", "weight": 0.05},
]


@pytest.fixture(autouse=True)
def _reset_turn_state():
    """Each test starts at roll 0 with no user text."""
    text_token = server_mod._active_user_text_ref.set("")
    roll_token = server_mod._active_roll_count_ref.set(0)
    yield
    server_mod._active_roll_count_ref.reset(roll_token)
    server_mod._active_user_text_ref.reset(text_token)


def _fake_result(choice="chat", laya_choice="observe", source="laya+prior") -> dict:
    posterior = {"observe": 0.4, "chat": 0.35, "recall": 0.2, "move": 0.05}
    return {
        "ok": True,
        "roll_id": "roll_deadbeefdeadbeef",
        "question": "接下来做什么？",
        "choice": choice,
        "confidence": posterior[choice],
        "shortlist": ["observe", "chat", "recall"],
        "posterior": posterior,
        "prior": {"observe": 0.6, "chat": 0.2, "recall": 0.15, "move": 0.05},
        "laya": {
            "choice": laya_choice,
            "probabilities": {},
            "confidence": 0.5,
            "source": "laya",
            "error": "",
        },
        "options": OPTIONS,
        "seed": 7,
        "source": source,
        "latency_ms": 123.4,
        "reason": "laya:observe@0.50|prior:0.35|sample|shortlist=observe,chat,recall",
    }


def _patch_roll(monkeypatch, fake) -> None:
    monkeypatch.setattr(server_mod._decision_sampler, "roll_decision", fake)


def _counters() -> dict:
    return telemetry.snapshot()["counters"]


@pytest.mark.asyncio
async def test_roll_handler_success_passes_turn_context_and_reports(monkeypatch):
    calls: list[dict] = []

    async def fake(question, options, **kwargs):
        calls.append({"question": question, "options": options, **kwargs})
        return _fake_result()

    _patch_roll(monkeypatch, fake)
    server_mod._active_user_text_ref.set("看看你左边有什么")

    before = _counters()
    result = await server_mod._decision_roll_tool({
        "question": "接下来做什么？",
        "options": OPTIONS,
        "context": "桌面上开着编辑器",
        "seed": 7,
    })

    assert len(calls) == 1
    call = calls[0]
    assert call["question"] == "接下来做什么？"
    assert call["options"] == OPTIONS
    assert call["context"] == "桌面上开着编辑器"
    assert call["seed"] == 7
    assert call["user_text"] == "看看你左边有什么"

    assert result["is_error"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["roll_id"] == "roll_deadbeefdeadbeef"
    assert payload["shortlist"] == ["observe", "chat", "recall"]
    # laya.choice differs from the sampled choice -> both are in the summary.
    assert result["summary"] == "掷骰 → chat (0.35, laya+prior) laya=observe"

    after = _counters()
    assert after.get("decision.roll.total", 0) == before.get("decision.roll.total", 0) + 1
    assert after.get("decision.roll.laya+prior", 0) == before.get("decision.roll.laya+prior", 0) + 1
    snapshot = telemetry.snapshot()
    assert "decision.roll.latency_ms" in snapshot["timings"]
    assert snapshot["gauges"]["decision.roll.confidence"] == pytest.approx(0.35)


@pytest.mark.asyncio
async def test_roll_handler_summary_hides_laya_when_it_agrees(monkeypatch):
    async def fake(question, options, **kwargs):
        return _fake_result(choice="observe", laya_choice="observe")

    _patch_roll(monkeypatch, fake)

    result = await server_mod._decision_roll_tool({
        "question": "接下来做什么？",
        "options": OPTIONS,
    })

    assert result["is_error"] is False
    assert result["summary"] == "掷骰 → observe (0.40, laya+prior)"
    assert "laya=" not in result["summary"]


@pytest.mark.asyncio
async def test_roll_handler_only_forwards_real_int_seed(monkeypatch):
    calls: list[dict] = []

    async def fake(question, options, **kwargs):
        calls.append(kwargs)
        return _fake_result()

    _patch_roll(monkeypatch, fake)

    for bad_seed in (True, "42", 1.5, None):
        args = {"question": "q", "options": OPTIONS}
        if bad_seed is not None:
            args["seed"] = bad_seed
        await server_mod._decision_roll_tool(args)

    assert all("seed" not in kwargs for kwargs in calls)

    server_mod._active_roll_count_ref.set(0)
    await server_mod._decision_roll_tool({
        "question": "q", "options": OPTIONS, "seed": 0,
    })
    assert calls[-1]["seed"] == 0


@pytest.mark.asyncio
async def test_roll_handler_value_error_is_soft(monkeypatch):
    calls: list[int] = []

    async def fake(question, options, **kwargs):
        calls.append(1)
        raise ValueError("options must contain 2..6 items (got 1)")

    _patch_roll(monkeypatch, fake)

    result = await server_mod._decision_roll_tool({
        "question": "接下来做什么？",
        "options": [OPTIONS[0]],
    })

    assert result["is_error"] is True
    assert "options must contain 2..6 items" in result["content"][0]["text"]
    assert "options must contain 2..6 items" in result["summary"]
    # A rejected roll does not burn the per-turn budget.
    assert server_mod._active_roll_count_ref.get() == 0


@pytest.mark.asyncio
async def test_roll_handler_invalid_args_use_real_sampler():
    """Missing fields are caught by the sampler; no HTTP, no crash."""
    result = await server_mod._decision_roll_tool({})

    assert result["is_error"] is True
    assert "question" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_roll_handler_caps_at_three_rolls_per_turn(monkeypatch):
    calls: list[int] = []

    async def fake(question, options, **kwargs):
        calls.append(1)
        return _fake_result()

    _patch_roll(monkeypatch, fake)

    for _ in range(3):
        result = await server_mod._decision_roll_tool({
            "question": "接下来做什么？",
            "options": OPTIONS,
        })
        assert result["is_error"] is False

    fourth = await server_mod._decision_roll_tool({
        "question": "接下来做什么？",
        "options": OPTIONS,
    })

    assert fourth["is_error"] is True
    assert "3" in fourth["content"][0]["text"]
    assert "commit" in fourth["summary"]
    assert len(calls) == 3, "the 4th roll must be refused without calling Laya"
    assert server_mod._active_roll_count_ref.get() == 3


@pytest.mark.asyncio
async def test_override_handler_logs_veto(monkeypatch):
    seen: list[tuple] = []

    def fake_override(roll_id, choice, reason, **kwargs):
        seen.append((roll_id, choice, reason))
        return {
            "ok": True,
            "roll_id": roll_id,
            "choice": choice,
            "reason": reason,
            "known": True,
            "roll": None,
        }

    monkeypatch.setattr(
        server_mod._decision_sampler, "record_override", fake_override
    )

    before = _counters().get("decision.override", 0)
    result = await server_mod._decision_override_tool({
        "roll_id": "roll_deadbeefdeadbeef",
        "choice": "observe",
        "reason": "用户在哭，先安慰",
    })

    assert seen == [("roll_deadbeefdeadbeef", "observe", "用户在哭，先安慰")]
    assert result["is_error"] is False
    assert result["summary"] == "否决已记录 → observe"
    payload = json.loads(result["content"][0]["text"])
    assert payload["known"] is True
    assert _counters().get("decision.override", 0) == before + 1


@pytest.mark.asyncio
async def test_override_handler_value_error_is_soft(monkeypatch):
    def fake_override(roll_id, choice, reason, **kwargs):
        raise ValueError("roll_id must be a non-empty string")

    monkeypatch.setattr(
        server_mod._decision_sampler, "record_override", fake_override
    )

    result = await server_mod._decision_override_tool({
        "roll_id": "", "choice": "observe", "reason": "x",
    })

    assert result["is_error"] is True
    assert "roll_id" in result["content"][0]["text"]


def test_core_tool_names_keep_the_decision_pair():
    assert {"memory", "recall", "memory_search"} <= server_mod.CORE_TOOL_NAMES
    assert {"pet_decision_roll", "pet_override_decision"} <= server_mod.CORE_TOOL_NAMES


@pytest.mark.asyncio
async def test_builtin_registration_is_listed_under_builtin_server():
    mcp = MCPManager(config_path=None)
    calls: list[dict] = []

    async def handler(args):
        calls.append(args)
        return {"content": [{"type": "text", "text": "ok"}], "is_error": False}

    mcp.register_builtin({
        "name": "pet_decision_roll",
        "description": "roll",
        "input_schema": {"type": "object", "properties": {}},
        "handler": handler,
    })

    tools = await mcp.list_all_tools()
    entry = next(t for t in tools if t["name"] == "pet_decision_roll")
    assert entry["server_name"] == "builtin"

    result = await mcp.call_tool("builtin", "pet_decision_roll", {"seed": 1})
    assert calls == [{"seed": 1}]
    assert result["content"][0]["text"] == "ok"


def test_native_and_text_calls_resolve_to_the_builtin_server():
    """Both routing paths used by the chat loop must land on ("builtin", name)."""
    tools = [
        server_mod.ToolDef(
            server_name="builtin",
            name="pet_decision_roll",
            description="roll",
            input_schema={"type": "object", "properties": {}},
        ),
        server_mod.ToolDef(
            server_name="builtin",
            name="pet_override_decision",
            description="override",
            input_schema={"type": "object", "properties": {}},
        ),
    ]
    tool_map = server_mod._build_tool_map(tools)

    # Native function calls arrive prefixed; models often emit the bare name.
    assert tool_map["mcp_builtin_pet_decision_roll"] == ("builtin", "pet_decision_roll")
    assert tool_map["pet_decision_roll"] == ("builtin", "pet_decision_roll")
    assert tool_map["pet_override_decision"] == ("builtin", "pet_override_decision")

    # Text markers parse to the same (server, name) pair.
    parsed = parse_mcp_calls(
        '[MCP:builtin/pet_decision_roll:{"question":"q","options":['
        '{"id":"observe","label":"看屏幕"},{"id":"chat","label":"聊天"}]}]'
    )
    assert len(parsed) == 1
    assert (parsed[0]["server_name"], parsed[0]["name"]) == (
        "builtin",
        "pet_decision_roll",
    )
    assert parsed[0]["arguments"]["options"][0]["id"] == "observe"

    builtin_calls = parse_mcp_calls(
        '[builtin/pet_override_decision:{"roll_id":"r","choice":"c","reason":"why"}]'
    )
    assert (builtin_calls[0]["server_name"], builtin_calls[0]["name"]) == (
        "builtin",
        "pet_override_decision",
    )


def test_native_dispatch_calls_the_registered_handler():
    """Simulate the chat loop: map a native call name then call_tool."""
    mcp = MCPManager(config_path=None)
    seen: list[dict] = []

    async def handler(args):
        seen.append(args)
        return {"content": [{"type": "text", "text": "rolled"}], "is_error": False}

    mcp.register_builtin({
        "name": "pet_decision_roll",
        "description": "roll",
        "input_schema": {"type": "object", "properties": {}},
        "handler": handler,
    })
    tool_map = server_mod._build_tool_map([
        server_mod.ToolDef(**t) for t in asyncio.run(mcp.list_all_tools())
    ])

    sn, tn = tool_map["mcp_builtin_pet_decision_roll"]
    result = asyncio.run(mcp.call_tool(sn, tn, {"seed": 3}))

    assert seen == [{"seed": 3}]
    assert result["content"][0]["text"] == "rolled"
