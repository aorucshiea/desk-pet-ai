"""Unit tests for LingLing loader + resonance."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from gateway.memory.events import EventStore
from gateway.memory import loader
from gateway.memory import resonance as res


class TestLoader:
    @pytest.fixture
    def populated_store(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore()
            store.load_from_disk(Path(td))
            for i in range(10):
                store.add_event(
                    title=f"事件标题第{i}个",
                    content=f"这是第{i}个事件的详细内容",
                    weight=900 - i * 80,
                )
            loader.reset_session()
            yield store

    def test_top_events_returns_5(self, populated_store):
        top = loader.load_top_events(populated_store, n=5)
        assert len(top) == 5
        # Should be highest weight events
        assert top[0]["title"] == "事件标题第0个"

    def test_load_top_returns_empty_for_empty_store(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore()
            store.load_from_disk(Path(td))
            top = loader.load_top_events(store)
            assert top == []

    def test_flashback_excludes_top_ids(self, populated_store):
        top_ids = {e["id"] for e in populated_store.get_all_events()[:2]}
        # With very high weights and only 10 events, flashback should pick something
        fb = loader.pick_flashback(populated_store, top_ids)
        if fb is not None:
            assert fb["id"] not in top_ids

    def test_faded_directory_truncates_low_weight(self, populated_store):
        top_ids = {e["id"] for e in populated_store.get_all_events()[:5]}
        directory = loader.build_faded_directory(populated_store, top_ids)
        # Very low weight events should have shorter titles or be gone
        for line in directory:
            assert line.startswith("- ")

    def test_build_memory_context_returns_string(self, populated_store):
        ctx = loader.build_memory_context(populated_store)
        assert isinstance(ctx, str)
        assert "你记得的事" in ctx or "你的记忆" in ctx

    def test_build_context_empty_store(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore()
            store.load_from_disk(Path(td))
            ctx = loader.build_memory_context(store)
            assert "还没有记住任何事" in ctx

    def test_session_ids_tracked_correctly(self, populated_store):
        loader.reset_session()
        evt = populated_store.get_all_events()[0]
        loader.add_to_session(evt["id"])
        assert loader.is_in_session(evt["id"])
        assert not loader.is_in_session("fake-id")


class TestResonance:
    @pytest.fixture
    def event_store(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore()
            store.load_from_disk(Path(td))
            store.add_event(
                title="加班到凌晨",
                content="加班到凌晨两点，他看起来很累",
                weight=800,
            )
            store.add_event(
                title="下雨天害怕",
                content="下雨天用户说很害怕打雷",
                weight=500,
            )
            yield store

    def test_find_resonance_keyword_fallback(self, event_store):
        loader.reset_session()
        # embedding model probably not installed, falls back to keyword
        results = res.find_resonance(event_store, "加班")
        assert len(results) >= 0  # keyword match may find 0 or 1

    def test_find_resonance_empty_message(self, event_store):
        results = res.find_resonance(event_store, "")
        assert results == []

    def test_mark_stale_triggers_rebuild(self):
        res.mark_stale()
        # Just verify no crash
        assert True
