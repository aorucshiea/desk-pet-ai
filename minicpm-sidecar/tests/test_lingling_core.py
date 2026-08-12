"""P5: core memory bank — nearly-immortal memories the model chooses.

User spec (2026-08-01):
  - a core memory bank where decay is nearly zero — 人类一直最关心的东西
  - model decides what goes in; promotion is the only way for a memory
    to get a big durability jump
  - cap ≤ 7 — putting something in means it almost never fades
  - core memories always loaded, presented as their own layer
    ("你一直放在心上的事")
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gateway.memory import decay as de
from gateway.memory import loader
from gateway.memory.events import EventStore


@pytest.fixture
def store(tmp_path: Path) -> EventStore:
    s = EventStore()
    s.load_from_disk(tmp_path)
    return s


class TestDecaySkipsCore:
    def test_core_memory_does_not_decay(self, store):
        evt = store.add_event(title="核心", content="c", weight=500, core=True)
        # 24h of passes.
        for _ in range(144):
            de.run_decay(store, last_conversation_at=None)
        assert store.get_event(evt["id"])["weight"] == 500

    def test_normal_memory_decays(self, store):
        evt = store.add_event(title="普通", content="c", weight=500)
        start = datetime.now(timezone.utc)
        step = timedelta(minutes=1)
        for i in range(20):
            # Simulate time passing: anchor last_decay_at in the past so
            # each pass bills real elapsed time (dt=0 would skip decay).
            evt = store.get_event(evt["id"])
            evt["last_decay_at"] = (start + step * i).isoformat()
            store.save()
            de.run_decay(store, now=start + step * (i + 1))
        assert store.get_event(evt["id"])["weight"] < 500


class TestCoreLayerInContext:
    def test_core_presented_as_own_layer(self, store):
        store.add_event(title="她说的第一句话", content="她问我叫什么（开心）", weight=900, core=True)
        store.add_event(title="普通事件", content="今天下雨（平淡）", weight=800)
        loader.reset_session()
        ctx = loader.build_memory_context(store)
        assert "【你一直放在心上的事】" in ctx
        assert "她说的第一句话" in ctx
        assert "【你记得的事】" in ctx
        # core appears in its own layer before ordinary memories
        assert ctx.index("你一直放在心上的事") < ctx.index("你记得的事")

    def test_no_core_no_layer(self, store):
        store.add_event(title="普通", content="c", weight=800)
        loader.reset_session()
        ctx = loader.build_memory_context(store)
        assert "【你一直放在心上的事】" not in ctx

    def test_core_loaded_even_when_low_weight(self, store):
        """Core wins a top seat over far heavier ordinary events."""
        store.add_event(title="心头事", content="c", weight=100, core=True)
        for i in range(10):
            store.add_event(title=f"重事件{i}", content="c", weight=900 - i)
        loader.reset_session()
        top = loader.load_top_events(store)
        assert "心头事" in {e["title"] for e in top}
