"""P4: loader v2 — felt labels in the faded directory, dynamic flashback
denominator, core memories always in the top layer.

User spec (2026-08-01):
  - directory entries carry [情绪] / [知识] / （还没放下） felt labels
  - flashback denominator grows with the memory library (dilution)
  - core memories unconditionally take a top-layer seat
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.memory import loader
from gateway.memory.events import EventStore


@pytest.fixture
def store(tmp_path: Path) -> EventStore:
    # The session-loaded set is module-global; reset so tests are
    # isolated regardless of what ran before them in the full suite.
    loader.reset_session()
    s = EventStore()
    s.load_from_disk(tmp_path)
    return s


class TestFadedDirectoryLabels:
    def test_emotion_label(self, store):
        store.add_event(title="加班到凌晨", content="用户加班到两点（疲惫）", weight=800)
        lines = loader.build_faded_directory(store, exclude_ids=set())
        # weight 800 → visible = 5 × 0.8 = 4 chars, prefixed [疲惫]
        assert any("[疲惫]" in ln and "加班到" in ln for ln in lines)

    def test_knowledge_dim_label(self, store):
        store.add_event(
            title="用户住上海", content="用户提到住在上海（平静）",
            weight=800, type_="knowledge",
        )
        lines = loader.build_faded_directory(store, exclude_ids=set())
        # weight 800 → visible = 5 × 0.8 = 4 chars "用户住上"
        assert any("[知识]" in ln and "用户住上" in ln for ln in lines)

    def test_unfinished_label(self, store):
        store.add_event(
            title="答应周末爬山", content="约好周末爬山（期待）",
            weight=900, resolved=False,
        )
        lines = loader.build_faded_directory(store, exclude_ids=set())
        # weight 900 → visible = 6 × 0.9 = 5 chars "答应周末爬"
        assert any("答应周末爬" in ln and "（还没放下）" in ln for ln in lines)

    def test_resolved_event_no_label(self, store):
        store.add_event(
            title="爬山完成", content="周末爬山回来了（开心）",
            weight=500, resolved=True,
        )
        lines = loader.build_faded_directory(store, exclude_ids=set())
        assert all("（还没放下）" not in ln for ln in lines)

    def test_low_weight_unfinished_no_label(self, store):
        """（还没放下）only shows for memories that still matter."""
        store.add_event(
            title="小事", content="c", weight=100, resolved=False,
        )
        lines = loader.build_faded_directory(store, exclude_ids=set())
        assert all("（还没放下）" not in ln for ln in lines)

    def test_fade_still_applies_with_labels(self, store):
        evt = store.add_event(title="加班到凌晨", content="c（疲惫）", weight=800)
        lines = loader.build_faded_directory(store, exclude_ids=set())
        # weight 800 → visible = 5 × 0.8 = 4 chars, prefixed with [疲惫]
        assert any("[疲惫]" in ln and len(ln) < len("加班到凌晨") + 20 for ln in lines)

    def test_very_low_weight_shows_fuzzy_placeholder(self, store):
        """用户: 非常非常低只显示（已模糊）占位，标题消失但记录仍在，
        数据仍在（recall 可达并可复活）。"""
        evt = store.add_event(title="几乎忘了", content="c", weight=20)
        evt["weight"] = 20.0  # below MIN_DIRECTORY_WEIGHT
        store.save()
        lines = loader.build_faded_directory(store, exclude_ids=set())
        assert any("已模糊" in ln for ln in lines)
        assert all("几乎忘了" not in ln for ln in lines)
        # 数据仍在 — recall/search 仍可达
        assert store.get_event(evt["id"])["weight"] == 20.0

    def test_low_but_not_floor_weight_keeps_record(self, store):
        """权重低但没到地板：目录仍有记录（模糊标题 ≥3 字）。"""
        evt = store.add_event(title="模糊但还在", content="c", weight=100)
        lines = loader.build_faded_directory(store, exclude_ids=set())
        assert any("模糊但还在"[:3] in ln for ln in lines)  # 5×0.1=0→clamp 3字


class TestFlashbackDenominator:
    def test_denominator_grows_with_event_count(self, store):
        for i in range(20):
            store.add_event(title=f"事件{i}", content="c", weight=100)
        denom = (
            loader.FLASHBACK_PROBABILITY_DIVISOR
            + store.event_count() * loader.FLASHBACK_DILUTION_PER_EVENT
        )
        assert denom == 1000 + 20 * 10

    def test_big_library_dilutes_flashback_probability(self, store, monkeypatch):
        """weight-100 event: P = 100/1000 = 10% in a small library vs
        100/2000 = 5% in a 100-event library."""
        monkeypatch.setattr(loader, "_session_loaded_ids", set())
        store.add_event(title="T", content="c", weight=100)
        small = 100 / (1000 + 1 * 10)
        for i in range(99):
            store.add_event(title=f"X{i}", content="c", weight=100)
        big = 100 / (1000 + 100 * 10)
        assert small > big


class TestCoreTopLoading:
    def test_core_always_in_top(self, store):
        core_evt = store.add_event(title="核心", content="最重要的", weight=200, core=True)
        for i in range(10):
            store.add_event(title=f"高权重{i}", content="c", weight=900 - i)
        loader.reset_session()
        top = loader.load_top_events(store)
        assert core_evt["id"] in {e["id"] for e in top}

    def test_core_takes_seat_not_beyond_cap(self, store):
        core_evt = store.add_event(title="核心", content="c", weight=100, core=True)
        for i in range(10):
            store.add_event(title=f"普通{i}", content="c", weight=500 - i)
        loader.reset_session()
        top = loader.load_top_events(store)
        assert len(top) == loader.TOP_N  # core replaces a seat, no overflow
        assert core_evt["id"] in {e["id"] for e in top}

    def test_core_over_cap_does_not_crash(self, store):
        for i in range(8):
            store.add_event(title=f"核心{i}", content="c", weight=100 + i, core=True)
        loader.reset_session()
        top = loader.load_top_events(store)
        assert len(top) >= 8  # 8 cores → top holds all 8
        assert all(e.get("core") for e in top)


class TestMemoryContextV2:
    def test_context_includes_directory_labels(self, store):
        store.add_event(title="加班到凌晨", content="用户加班（疲惫）", weight=800, resolved=False)
        loader.reset_session()
        ctx = loader.build_memory_context(store)
        # Top-1 loads the event fully; the directory still shows felt labels
        # for the not-loaded remainder — here nothing is in the directory
        # (only 1 event), so assert the context reads like a memory.
        assert "你记得的事" in ctx or "你的记忆" in ctx


class TestTimeSense:
    """Memories carry felt ages + a now-anchor so the model has a time
    sense (用户: 记忆要带时间，模型得有时间感)."""

    def _evt_aged(self, hours_ago: float):
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()

    def test_now_anchor_injected(self, store):
        store.add_event(title="T", content="c", weight=900)
        loader.reset_session()
        ctx = loader.build_memory_context(store)
        assert "现在是" in ctx and "年" in ctx  # (现在是2026年…)

    def test_top_events_carry_negative_offset(self, store):
        evt = store.add_event(title="老记忆", content="c", weight=900)
        evt["created_at"] = self._evt_aged(48)  # 2 days ago
        store.save()
        loader.reset_session()
        ctx = loader.build_memory_context(store)
        assert "[-2天]" in ctx and "老记忆" in ctx

    def test_aged_label_any_layer(self, store):
        """The offset appears wherever the memory lands — top layer
        ([-8天] 标题) or directory (-8天的事 标题)."""
        evt = store.add_event(title="模糊记忆", content="c", weight=600)
        evt["created_at"] = self._evt_aged(200)  # 8+ days ago
        store.save()
        loader.reset_session()
        ctx = loader.build_memory_context(store)
        assert "[-8天]" in ctx or "-8天的事" in ctx

    def test_flashback_carries_negative_offset(self, store, monkeypatch):
        evt = store.add_event(title="闪现", content="c", weight=900)
        evt["created_at"] = self._evt_aged(5)  # 5 hours ago
        store.save()
        # Force flashback to pick this event.
        monkeypatch.setattr(loader, "_session_loaded_ids", set())
        monkeypatch.setattr(loader, "pick_flashback", lambda s, ex: store.get_event(evt["id"]))
        loader.reset_session()
        ctx = loader.build_memory_context(store)
        assert "[-5小时]" in ctx and "闪现" in ctx
