"""P2: probabilistic recall — subconscious sampling, not a query.

User spec (2026-08-01):
  - recall(开心) retrieves MANY happy candidates, each surfaced with
    probability tied to its own weight (weight-10 → ~1%, weight-900 → ~90%)
  - the miss is human ("想不起来"), repeated attempts gradually recall more
  - the reply always says when the memory is from (昨天/很久以前)
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from gateway.memory import loader
from gateway.memory.events import EventStore
from gateway.memory import recall as rc


@pytest.fixture
def store(tmp_path) -> EventStore:
    s = EventStore()
    s.load_from_disk(tmp_path)
    return s


@pytest.fixture
def seeded_store(tmp_path) -> EventStore:
    """A store with text + emotion matches of varying weights."""
    s = EventStore()
    s.load_from_disk(tmp_path)
    s.add_event(
        title="加班到凌晨", content="用户加班到两点（疲惫），我说该睡了（难过）",
        weight=900, emotion="疲惫", type_="experience",
    )
    s.add_event(
        title="周末爬山", content="一起去爬山（开心），山顶风很大（兴奋）",
        weight=600, emotion="开心", type_="experience",
    )
    s.add_event(
        title="丢钥匙", content="钥匙不见了（难过），最后在沙发里找到（开心）",
        weight=50, emotion="难过", type_="experience",
    )
    return s


class TestSearchCandidates:
    def test_emotion_channel_ranks_first(self, seeded_store):
        cands = rc._search_candidates(seeded_store, "开心")
        assert cands and cands[0]["title"] == "周末爬山"  # exact emotion match first

    def test_text_channel(self, seeded_store):
        cands = rc._search_candidates(seeded_store, "加班")
        titles = {c["title"] for c in cands}
        assert "加班到凌晨" in titles

    def test_emotion_and_text_union(self, seeded_store):
        cands = rc._search_candidates(seeded_store, "难过")
        titles = {c["title"] for c in cands}
        assert "丢钥匙" in titles      # emotion match
        assert "加班到凌晨" in titles  # text match (content has 难过 tag)
        assert len(cands) >= 2

    def test_no_match_empty(self, seeded_store):
        assert rc._search_candidates(seeded_store, "不存在的东西") == []


class TestSampling:
    def test_high_weight_almost_always_surfaces(self, seeded_store):
        random.seed(1)
        cands = rc._search_candidates(seeded_store, "加班")
        hits = 0
        for _ in range(200):
            loader.reset_session()
            sampled = rc._sample_recall(seeded_store, cands)
            if sampled:
                hits += 1
        assert hits > 190  # weight-900 → ~90% per roll

    def test_low_weight_rarely_surfaces(self, seeded_store):
        random.seed(2)
        cands = rc._search_candidates(seeded_store, "丢钥匙")
        hits = 0
        for _ in range(200):
            loader.reset_session()
            sampled = rc._sample_recall(seeded_store, cands)
            if sampled:
                hits += 1
        assert 0 <= hits < 40  # weight-50 → ~5%

    def test_session_dedup(self, seeded_store):
        random.seed(3)
        cands = rc._search_candidates(seeded_store, "加班")
        loader.reset_session()
        first = rc._sample_recall(seeded_store, cands)
        assert first  # surfaced at least once
        again = rc._sample_recall(seeded_store, cands)
        # The surfaced event must not come back a second time.
        for evt in first:
            assert evt["id"] not in {e["id"] for e in again}

    def test_zero_weight_never_surfaces(self, store):
        evt = store.add_event(title="ghost", content="c", weight=100)
        evt["weight"] = 0
        store.save()
        cands = rc._search_candidates(store, "ghost")
        assert rc._sample_recall(store, cands) == []


class TestHumanTimeAgo:
    def _iso(self, hours_ago: float) -> str:
        return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()

    def test_phrases(self):
        # 负数偏移：距现在过去了多久（不与'昨天/今天'混淆）
        from datetime import datetime, timedelta, timezone
        just = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        assert rc.human_time_ago(just) == "-刚刚"
        assert rc.human_time_ago(self._iso(0.5)) == "-30分钟"
        assert rc.human_time_ago(self._iso(6)) == "-6小时"
        assert rc.human_time_ago(self._iso(20)) == "-20小时"
        assert rc.human_time_ago(self._iso(50)) == "-2天"
        assert rc.human_time_ago(self._iso(4 * 24)) == "-4天"
        assert rc.human_time_ago(self._iso(3 * 7 * 24)) == "-21天"
        assert rc.human_time_ago(self._iso(2 * 30 * 24)) == "-2个月"
        assert rc.human_time_ago(self._iso(400 * 24)) == "-很久"

    def test_garbage_falls_back(self):
        assert rc.human_time_ago("not-a-date") == "-很久"


class TestCoreRecallBonus:
    """核心长期记忆 recall 概率 = 普通权重 + 核心加成 (CORE_RECALL_BONUS)."""

    @pytest.mark.asyncio
    async def test_core_event_gets_bonus_probability(self, store, monkeypatch):
        monkeypatch.setattr(rc, "_event_store", store)
        evt = store.add_event(title="T", content="c（平静）", weight=10, core=True)
        loader.reset_session()
        # base = 0.01 + 0.40 = 0.41 → roll 0.3 hits, roll 0.5 misses.
        monkeypatch.setattr(random, "random", lambda: 0.3)
        out = await rc.recall_tool_handler({"keyword": "平静"})
        assert "你想起来了" in out["content"][0]["text"]

        loader.reset_session()
        monkeypatch.setattr(random, "random", lambda: 0.5)
        out = await rc.recall_tool_handler({"keyword": "平静"})
        assert "抓不住" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_non_core_event_no_bonus(self, store, monkeypatch):
        monkeypatch.setattr(rc, "_event_store", store)
        store.add_event(title="T", content="c（平静）", weight=10, core=False)
        loader.reset_session()
        monkeypatch.setattr(random, "random", lambda: 0.3)
        out = await rc.recall_tool_handler({"keyword": "平静"})
        assert "抓不住" in out["content"][0]["text"]  # 0.01 < 0.3 → miss


class TestHandler:
    @pytest.mark.asyncio
    async def test_handler_returns_sampled_with_time(self, seeded_store, monkeypatch):
        monkeypatch.setattr(rc, "_event_store", seeded_store)
        loader.reset_session()
        # Force the 900-weight event through.
        out = await rc.recall_tool_handler({"keyword": "加班"})
        assert out["is_error"] is False
        text = out["content"][0]["text"]
        assert "你想起来了" in text
        assert "的事：" in text  # time annotation

    @pytest.mark.asyncio
    async def test_handler_zero_hit_returns_human_miss(self, store, monkeypatch):
        monkeypatch.setattr(rc, "_event_store", store)
        store.add_event(title="T", content="c（平静）", weight=1)
        # Deterministic miss: every roll loses (weight-1 event, P=0.1%).
        monkeypatch.setattr(random, "random", lambda: 0.99)
        # Isolate: event ids collide across stores (same date+seq), which
        # would otherwise put this event in the session from an earlier test.
        loader.reset_session()
        out = await rc.recall_tool_handler({"keyword": "平静"})
        assert out["is_error"] is False
        assert "抓不住" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_handler_no_candidates_tells_model_why(self, store, monkeypatch):
        monkeypatch.setattr(rc, "_event_store", store)
        loader.reset_session()
        out = await rc.recall_tool_handler({"keyword": "不存在的词"})
        assert out["is_error"] is False
        assert "没有找到" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_handler_retry_bonus_makes_miss_followed_by_hit(self, store, monkeypatch):
        monkeypatch.setattr(rc, "_event_store", store)
        store.add_event(title="T", content="c（平静）", weight=10)
        loader.reset_session()
        # Force miss first (P=1%), then force hit (attempt bonus pushes p to 1.0).
        monkeypatch.setattr(random, "random", lambda: 0.999)
        out1 = await rc.recall_tool_handler({"keyword": "平静"})
        assert "抓不住" in out1["content"][0]["text"]
        monkeypatch.setattr(random, "random", lambda: 0.0)  # always roll low → hit
        out2 = await rc.recall_tool_handler({"keyword": "平静"})
        assert "你想起来了" in out2["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_handler_no_store(self, monkeypatch):
        monkeypatch.setattr(rc, "_event_store", None)
        out = await rc.recall_tool_handler({"keyword": "x"})
        assert out["is_error"] is True
