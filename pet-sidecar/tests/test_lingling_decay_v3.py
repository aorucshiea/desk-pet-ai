"""P0.2: decay engine v3 — shrinking rate, weight floor, conversation
freeze, 24h calibration, uncapped growth.

User spec (2026-08-01):
  - 刚开始忘得最快，后来越来越慢（每分钟衰减速度变更小），不能到 0
  - 用户不跟 ai 聊天了（不活跃），记忆冻结，不再衰减
  - 24 小时（活跃对话期内）一个权重 100 的事几乎忘干净（< 1）
  - 权重动态增长不封顶（初始自评 1-999，反复想起可超过）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gateway.memory import decay as de
from gateway.memory.events import EventStore


@pytest.fixture
def store(tmp_path) -> EventStore:
    s = EventStore()
    s.load_from_disk(tmp_path)
    return s


def _age_now(store: EventStore, evt_id: str, hours: float) -> None:
    """Rewind created_at / last_decay_at so the event looks `hours` old."""
    evt = store.get_event(evt_id)
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    evt["created_at"] = ts
    evt["last_decay_at"] = ts
    store.save()


class TestRateCurve:
    def test_fastest_at_birth(self):
        assert de.rate_at_age(0) == de.DECAY_RATE0_PER_HOUR

    def test_rate_shrinks_with_age(self):
        r0 = de.rate_at_age(0)
        r1 = de.rate_at_age(1)
        r10 = de.rate_at_age(10)
        assert r0 > r1 > r10
        assert r10 == de.DECAY_RATE_MIN_PER_HOUR  # hits the floor

    def test_rate_has_floor_never_zero(self):
        for hours in (24, 100, 1000):
            assert de.rate_at_age(hours) == de.DECAY_RATE_MIN_PER_HOUR

    def test_compute_decay_scales_with_elapsed_time(self):
        small = de.compute_decay(100, 0.5, 0, 1.0)
        large = de.compute_decay(100, 1.0, 0, 1.0)
        assert large == pytest.approx(small * 2)


class TestDecayPass:
    def test_reduces_weight(self, store):
        evt = store.add_event(title="T", content="C", weight=500)
        _age_now(store, evt["id"], 2)  # rate now at the floor
        de.run_decay(store, last_conversation_at=datetime.now(timezone.utc))
        assert store.get_event(evt["id"])["weight"] < 500

    def test_weight_never_below_floor(self, store):
        evt = store.add_event(title="T", content="C", weight=100)
        _age_now(store, evt["id"], 24)
        # 24h of decay in one pass — must clamp at W_MIN, not go to 0.
        de.run_decay(store, last_conversation_at=datetime.now(timezone.utc))
        assert store.get_event(evt["id"])["weight"] >= de.W_MIN

    def test_24h_calibration_100_to_below_1(self, store):
        """Simulate 144 × 10-minute passes over 24h of active time.

        An unconsolidated weight-100 event must end below 1 (almost
        forgotten) but above the 0.5 floor (never zero).
        """
        evt = store.add_event(title="T", content="C", weight=100)
        start = datetime.now(timezone.utc)
        # Event just born, decay baseline anchored at t0.
        _age_now(store, evt["id"], 0)
        step = timedelta(minutes=10)
        for i in range(144):
            # Simulate one 10-minute pass with injected "now". The
            # conversation timestamp moves with "now" so the pet stays
            # awake the whole 24h (freeze is tested separately).
            evt = store.get_event(evt["id"])
            evt["last_decay_at"] = (start + step * i).isoformat()
            store.save()
            de.run_decay(
                store,
                last_conversation_at=start + step * (i + 1),
                now=start + step * (i + 1),
            )
        w = store.get_event(evt["id"])["weight"]
        assert w < 1.0, f"expected <1 after 24h, got {w}"
        assert w >= de.W_MIN

    def test_pause_decay_skips_event(self, store):
        evt = store.add_event(title="Paused", content="test", weight=500)
        _age_now(store, evt["id"], 2)
        evt["pause_decay"] = True
        store.save()
        de.run_decay(store, last_conversation_at=datetime.now(timezone.utc))
        assert store.get_event(evt["id"])["weight"] == 500

    def test_core_skipped(self, store):
        evt = store.add_event(title="Core", content="test", weight=500)
        _age_now(store, evt["id"], 2)
        evt["core"] = True
        store.save()
        de.run_decay(store, last_conversation_at=datetime.now(timezone.utc))
        assert store.get_event(evt["id"])["weight"] == 500

    def test_freeze_without_recent_conversation(self, store):
        evt = store.add_event(title="T", content="C", weight=500)
        _age_now(store, evt["id"], 2)
        idle = datetime.now(timezone.utc) - timedelta(hours=de.FREEZE_HOURS + 1)
        assert de.run_decay(store, last_conversation_at=idle) == 0
        assert store.get_event(evt["id"])["weight"] == 500

    def test_no_freeze_when_conversation_recent(self, store):
        evt = store.add_event(title="T", content="C", weight=500)
        _age_now(store, evt["id"], 2)
        de.run_decay(store, last_conversation_at=datetime.now(timezone.utc))
        assert store.get_event(evt["id"])["weight"] < 500

    def test_no_last_conversation_means_no_freeze(self, store):
        """Backward-compat: callers that don't pass the conversation
        timestamp (e.g. unit tests, curator) get plain decay."""
        evt = store.add_event(title="T", content="C", weight=500)
        _age_now(store, evt["id"], 2)
        de.run_decay(store)
        assert store.get_event(evt["id"])["weight"] < 500


class TestUncappedGrowth:
    def test_weight_growth_not_clamped_at_999(self, store):
        evt = store.add_event(title="T", content="C", weight=990)
        for _ in range(10):
            de.on_event_accessed(store, evt["id"])
        assert store.get_event(evt["id"])["weight"] > 999

    def test_mention_also_uncapped(self, store):
        evt = store.add_event(title="T", content="C", weight=998)
        de.on_event_mentioned(store, evt["id"])
        assert store.get_event(evt["id"])["weight"] == 1000
