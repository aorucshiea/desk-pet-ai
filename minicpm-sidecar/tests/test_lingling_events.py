"""Unit tests for LingLing EventStore — episodic weighted memory."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from gateway.memory.events import EventStore


class TestEventStore:
    """Core CRUD and persistence tests for the event store."""

    @pytest.fixture
    def tmp_store(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore()
            store.load_from_disk(Path(td))
            yield store

    def test_empty_store_has_zero_events(self, tmp_store):
        assert tmp_store.event_count() == 0
        assert tmp_store.total_weight() == 0

    def test_add_event_populates_all_fields(self, tmp_store):
        evt = tmp_store.add_event(
            title="加班到凌晨两点",
            content="他一直在改Excel，看起来很累。",
            weight=850,
        )
        assert evt["id"].startswith("evt_")
        assert evt["title"] == "加班到凌晨两点"
        assert evt["weight"] == 850
        assert evt["decay_coefficient"] == 1.0
        assert evt["access_count"] == 0
        assert evt["pause_decay"] is False
        assert "created_at" in evt
        assert "last_accessed" in evt

    def test_event_count_matches(self, tmp_store):
        tmp_store.add_event(title="A", content="test a", weight=500)
        tmp_store.add_event(title="B", content="test b", weight=300)
        assert tmp_store.event_count() == 2

    def test_total_weight_sums_correctly(self, tmp_store):
        tmp_store.add_event(title="A", content="test", weight=600)
        tmp_store.add_event(title="B", content="test", weight=400)
        assert tmp_store.total_weight() == 1000

    def test_weight_clamped_to_1_999(self, tmp_store):
        hi = tmp_store.add_event(title="Too high", content="test", weight=9999)
        lo = tmp_store.add_event(title="Too low", content="test", weight=-5)
        assert hi["weight"] == 999
        assert lo["weight"] == 1

    def test_remove_event(self, tmp_store):
        evt = tmp_store.add_event(title="Remove me", content="test", weight=100)
        assert tmp_store.event_count() == 1
        assert tmp_store.remove_event(evt["id"]) is True
        assert tmp_store.event_count() == 0
        assert tmp_store.remove_event("nonexistent") is False

    def test_get_event_by_id(self, tmp_store):
        evt = tmp_store.add_event(title="T", content="C", weight=500)
        found = tmp_store.get_event(evt["id"])
        assert found is not None
        assert found["title"] == "T"
        assert tmp_store.get_event("bad-id") is None

    def test_keyword_search(self, tmp_store):
        tmp_store.add_event(title="加班", content="加班到凌晨两点", weight=800)
        tmp_store.add_event(title="下雨", content="下雨天用户很害怕", weight=500)
        tmp_store.add_event(title="午饭", content="今天吃了拉面", weight=200)
        results = tmp_store.search_by_keyword("加班")
        assert len(results) == 1
        assert results[0]["title"] == "加班"
        results2 = tmp_store.search_by_keyword("下雨")
        assert results2[0]["title"] == "下雨"

    def test_persistence_roundtrip(self, tmp_store):
        tmp_store.add_event(title="Persist me", content="Will survive", weight=700)
        
        # Create a new store instance pointing to the same dir
        store2 = EventStore()
        store2.load_from_disk(Path(tmp_store._memory_dir))
        assert store2.event_count() == 1
        evt = store2.get_all_events()[0]
        assert evt["title"] == "Persist me"
        assert evt["weight"] == 700

    def test_normalize_event_fixes_missing_fields(self, tmp_store):
        # Simulate old-format event from JSON
        tmp_store._events.append({"title": "Old", "weight": 500, "decay_coefficient": 999})
        tmp_store._normalize_event(tmp_store._events[0])
        evt = tmp_store._events[0]
        assert "id" in evt
        assert "content" in evt
        assert evt["decay_coefficient"] == 999.0  # high values preserved (min floor only)
        assert evt["decay_coefficient"] >= 0.05
        assert evt["weight"] == 500  # floats stored as ints
        assert evt["pause_decay"] is False
        assert evt["access_count"] == 0

    def test_title_truncated_to_15_chars(self, tmp_store):
        evt = tmp_store.add_event(
            title="这是一个非常非常非常长的标题超过了十五个字",
            content="test",
            weight=100,
        )
        assert len(evt["title"]) <= 15
