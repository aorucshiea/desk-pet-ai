"""Decision layer — intuition -> inner voice ("button") -> ephemeral memory.

The 2026-10-19 clarification: 心声 is not a decision. It is an optional
button the pet may press or ignore. These tests pin:
  * a tired user produces an "emotion" button, not "想活动一下";
  * weak signals produce no button at all (quiet is the default);
  * conflicting strong signals produce hesitation;
  * the loop writes the voice into ephemeral memory and enforces the 1h reset;
  * body log records real actions in first person.
"""

from __future__ import annotations

import asyncio
from typing import Any

from gateway.decision.body_log import BodyLog
from gateway.decision.dice import roll
from gateway.decision.integrate import integrate
from gateway.decision.loop import DecisionLoop
from gateway.decision.meta import needs_decision
from gateway.decision.registry import Intuition, IntuitionRegistry
from gateway.memory.ephemeral import EphemeralMemory


class AlwaysRng:
    def random(self) -> float:
        return 0.0

    def shuffle(self, values) -> None:
        return None

    def choices(self, labels, weights, k=1):
        return [labels[0]]


def _tired_state() -> dict:
    return {
        "last_user_msg": "我好累",
        "mood_index": -0.3,
        "idle_sec": 0,
        "bodies": [],
        "recent_actions": 0,
    }


def test_meta_flags_tired_user():
    needs = needs_decision(_tired_state())
    assert needs["boost"] == 0.9
    assert "emotion" in needs["focus"]
    assert "negative_mood" in needs["signals"]


def test_integrate_emotion_is_a_button_not_an_activity_decision():
    result = integrate(IntuitionRegistry(), _tired_state(), needs_decision(_tired_state()), rng=AlwaysRng())

    assert result["status"] == "ok"
    assert result["kind"] == "emotion"
    assert "在意" in result["voice"]
    assert "想活动一下" not in result["voice"]


def test_integrate_space_voice_when_a_body_is_near():
    state = {
        "last_user_msg": "",
        "mood_index": 0,
        "idle_sec": 0,
        "bodies": [{"id": 1, "label": "notepad", "dist": 120}],
        "recent_actions": 0,
    }
    result = integrate(IntuitionRegistry(), state, {"boost": 0.1, "focus": []}, rng=AlwaysRng())

    assert result["status"] == "ok"
    assert result["kind"] == "space"
    assert "旁边" in result["voice"]


def test_integrate_quiet_when_every_signal_is_weak():
    state = {"last_user_msg": "", "mood_index": 0, "idle_sec": 0, "bodies": [], "recent_actions": 0}
    result = integrate(IntuitionRegistry(), state, {"boost": 0.0, "focus": []}, rng=AlwaysRng())
    assert result["status"] == "quiet"
    assert result["voice"] == ""


def test_integrate_conflicting_strong_signals_hesitates():
    registry = IntuitionRegistry()
    registry._items.clear()  # test-only: isolate from builtins
    registry.register(Intuition("a", "habit", "", lambda s: 0.9, p_emit=1.0, weight=1.0))
    registry.register(Intuition("b", "space", "", lambda s: 0.4, p_emit=1.0, weight=1.0))

    result = integrate(registry, {}, {"boost": 0.0, "focus": []}, rng=AlwaysRng())

    assert result["status"] == "ok"
    assert result["kind"] == "conflict"
    assert "犹豫" in result["voice"]


def test_dice_three_forms():
    assert roll({"type": "binary", "p": 1.0, "yes": "走", "no": "停"}, rng=AlwaysRng())["label"] == "走"
    assert roll({"type": "choice", "options": ["左", "右"]}, rng=AlwaysRng())["result"] == "左"
    assert roll({"type": "weighted", "options": {"走": 1.0}}, rng=AlwaysRng())["result"] == "走"


def test_ephemeral_hourly_reset():
    eph = EphemeralMemory()
    eph.add_thought("一个念头")
    eph._last_reset -= eph.RESET_SECONDS + 1

    assert eph.maybe_reset() is True
    assert eph.thoughts == []
    assert eph.format_for_prompt() == ""


def test_ephemeral_dedupes_repeated_thought():
    eph = EphemeralMemory()
    eph.add_thought("同一个念头")
    eph.add_thought("同一个念头")
    assert len(eph.thoughts) == 1


def test_ephemeral_prompt_frames_voice_as_optional_button():
    eph = EphemeralMemory()
    eph.add_thought("有点在意用户现在的状态", source="emotion", confidence=0.6)

    text = eph.format_for_prompt()
    assert "不是命令" in text
    assert "可按可不按" in text
    assert "有点在意用户现在的状态" in text


def test_ephemeral_dir_snapshot_copies_index(tmp_path):
    mem = tmp_path / "记忆"
    mem.mkdir()
    (mem / "a.md").write_text("x", encoding="utf-8")
    eph = EphemeralMemory(tmp_path)
    eph.refresh_dir_snapshot()
    assert "记忆/a.md" in eph.dir_snapshot


def test_body_log_first_person():
    log = BodyLog()
    log.append("walk", "dx=-80, dy=0")
    log.append("push", "id=3")
    text = log.format_for_prompt()
    assert "你走动了" in text
    assert "你推开了" in text


def test_loop_writes_voice_and_calls_reset():
    class Registry:
        def items(self):
            return {"emotion": Intuition("emotion", "emotion", "", lambda s: 0.9, p_emit=1.0, weight=1.0)}

    class Eph:
        def __init__(self):
            self.thoughts = []
            self.reset_calls = 0

        def maybe_reset(self):
            self.reset_calls += 1
            return False

        def refresh_dir_snapshot(self):
            pass

        def add_thought(self, text, source="x", confidence=0.0):
            self.thoughts.append((text, source, confidence))

    eph = Eph()
    loop = DecisionLoop(Registry(), eph, lambda: {"last_user_msg": "我好累"}, interval_sec=0)

    def collect():
        loop._stop = True
        return {"last_user_msg": "我好累"}

    loop.collect_state = collect
    asyncio.run(loop._run())

    assert eph.reset_calls >= 1
    assert eph.thoughts and "在意" in eph.thoughts[0][0]


def test_loop_supports_async_collect():
    class Registry:
        def items(self):
            return {}

    class Eph:
        def maybe_reset(self):
            return False

        def refresh_dir_snapshot(self):
            pass

        def add_thought(self, *a, **k):
            raise AssertionError("no voice expected")

    loop = DecisionLoop(Registry(), Eph(), lambda: {}, interval_sec=0)

    async def collect():
        loop._stop = True
        return {"last_user_msg": ""}

    loop.collect_state = collect
    asyncio.run(loop._run())
