"""Unit tests for LingLing decay engine."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from gateway.memory.events import EventStore
from gateway.memory import decay as de


class TestDecay:
    @pytest.fixture
    def fresh_store(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore()
            store.load_from_disk(Path(td))
            yield store

    def test_decay_reduces_weight(self, fresh_store):
        evt = fresh_store.add_event(title="T", content="C", weight=500)
        evt["last_accessed"] = "2000-01-01T00:00:00"  # 26 years ago
        de.run_decay(fresh_store)
        updated = fresh_store.get_event(evt["id"])
        assert updated["weight"] < 500

    def test_decay_already_zero_stays_zero(self, fresh_store):
        evt = fresh_store.add_event(title="Zero", content="test", weight=0)
        evt["last_accessed"] = "2000-01-01T00:00:00"
        de.run_decay(fresh_store)
        assert fresh_store.get_event(evt["id"])["weight"] == 0

    def test_pause_decay_skips_event(self, fresh_store):
        evt = fresh_store.add_event(title="Paused", content="test", weight=500)
        evt["last_accessed"] = "2000-01-01T00:00:00"
        evt["pause_decay"] = True
        de.run_decay(fresh_store)
        assert fresh_store.get_event(evt["id"])["weight"] == 500  # unchanged

    def test_on_event_accessed_boosts_weight(self, fresh_store):
        evt = fresh_store.add_event(title="T", content="C", weight=100)
        de.on_event_accessed(fresh_store, evt["id"])
        updated = fresh_store.get_event(evt["id"])
        assert updated["weight"] == 102  # +2
        assert updated["access_count"] == 1
        assert updated["decay_coefficient"] < 1.0  # consolidated

    def test_on_event_mentioned_pauses_decay(self, fresh_store):
        evt = fresh_store.add_event(title="T", content="C", weight=100)
        de.on_event_mentioned(fresh_store, evt["id"])
        updated = fresh_store.get_event(evt["id"])
        assert updated["pause_decay"] is True
        assert updated["weight"] == 102  # +2

    def test_on_conversation_end_unpauses_and_consolidates(self, fresh_store):
        evt = fresh_store.add_event(title="T", content="C", weight=100)
        evt["pause_decay"] = True
        old_coef = evt["decay_coefficient"]
        de.on_conversation_end(fresh_store)
        updated = fresh_store.get_event(evt["id"])
        assert updated["pause_decay"] is False
        assert updated["decay_coefficient"] < old_coef

    def test_consolidation_has_floor(self, fresh_store):
        evt = fresh_store.add_event(title="T", content="C", weight=100)
        # Access 50 times — should bottom out at 0.05
        for _ in range(50):
            de.on_event_accessed(fresh_store, evt["id"])
        assert fresh_store.get_event(evt["id"])["decay_coefficient"] >= 0.05
