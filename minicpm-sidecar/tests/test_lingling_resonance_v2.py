"""P3: resonance v2 — API embedding subconscious layer with fallbacks.

User spec (2026-08-01):
  - embedding via free API (SiliconFlow bge-m3), NOT local torch
  - vectors cached locally; incremental embedding of new events only
  - no key / network failure → degrade to emotion match → keyword match,
    never raise
  - injection block uses felt language + time annotation
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gateway.memory import loader, resonance as rs
from gateway.memory.events import EventStore


@pytest.fixture
def store(tmp_path) -> EventStore:
    s = EventStore()
    s.load_from_disk(tmp_path)
    return s


@pytest.fixture
def seeded_store(tmp_path) -> EventStore:
    s = EventStore()
    s.load_from_disk(tmp_path)
    evt = s.add_event(
        title="加班到凌晨", content="用户加班到两点（疲惫），我说该睡了（难过）",
        weight=800, emotion="疲惫",
    )
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    evt["created_at"] = old
    s.save()
    return s


class TestVectorStore:
    def test_set_vector_store_loads_and_saves(self, tmp_path, monkeypatch):
        rs.set_vector_store(tmp_path)
        assert rs._vectors == {}
        rs._vectors["evt_1"] = [0.1, 0.2]
        rs._save_vectors()
        rs2 = rs
        rs2._vectors = {}
        rs2.set_vector_store(tmp_path)
        assert rs2._vectors.get("evt_1") == [0.1, 0.2]


class TestFallbacks:
    @pytest.mark.asyncio
    async def test_no_key_uses_emotion_fallback(self, seeded_store, monkeypatch):
        monkeypatch.delenv("MINICPM_EMBEDDING_API_KEY", raising=False)
        monkeypatch.setattr(rs, "_api_key", lambda: None)
        loader.reset_session()
        hits = await rs.find_resonance(seeded_store, "今天太疲惫了")
        assert hits and hits[0]["title"] == "加班到凌晨"
        assert loader.is_in_session(hits[0]["id"])

    @pytest.mark.asyncio
    async def test_emotion_fallback_returns_empty_without_emotion_word(self, seeded_store, monkeypatch):
        monkeypatch.setattr(rs, "_api_key", lambda: None)
        loader.reset_session()
        hits = await rs.find_resonance(seeded_store, "好累啊")
        assert hits == []  # no vocab emotion word → keyword fallback also misses

    @pytest.mark.asyncio
    async def test_keyword_fallback(self, store, monkeypatch):
        monkeypatch.setattr(rs, "_api_key", lambda: None)
        store.add_event(title="钥匙丢了", content="在沙发里找到（开心）", weight=300)
        loader.reset_session()
        hits = await rs.find_resonance(store, "钥匙")
        assert hits and hits[0]["title"] == "钥匙丢了"

    @pytest.mark.asyncio
    async def test_api_failure_degrades_to_emotion(self, seeded_store, monkeypatch):
        monkeypatch.setenv("MINICPM_EMBEDDING_API_KEY", "test-key")

        async def _fail(text):
            return None

        monkeypatch.setattr(rs, "_embed_text", _fail)
        loader.reset_session()
        hits = await rs.find_resonance(seeded_store, "今天太疲惫了")
        assert hits and hits[0]["title"] == "加班到凌晨"  # emotion fallback


class TestEmbeddingPath:
    @pytest.mark.asyncio
    async def test_embedding_hit_and_consolidation(self, store, monkeypatch):
        monkeypatch.setenv("MINICPM_EMBEDDING_API_KEY", "test-key")
        evt = store.add_event(title="加班到凌晨", content="用户加班到两点（疲惫）", weight=800)
        loader.reset_session()

        # Deterministic fake embeddings: user msg "好累" is closest to the event.
        fake = {"event": [1.0, 0.0], "msg": [0.95, 0.05], "other": [0.0, 1.0]}
        calls = []

        async def fake_embed(text):
            calls.append(text)
            if "加班" in text or "疲惫" in text:
                return fake["event"]
            if "好累" in text:
                return fake["msg"]
            return fake["other"]

        monkeypatch.setattr(rs, "_embed_text", fake_embed)
        hits = await rs.find_resonance(store, "好累")
        assert len(hits) == 1
        assert hits[0]["id"] == evt["id"]
        assert loader.is_in_session(evt["id"])
        # surfaced → consolidated
        assert store.get_event(evt["id"])["access_count"] == 1
        # vectors cached for the event
        assert evt["id"] in rs._vectors

    @pytest.mark.asyncio
    async def test_low_similarity_does_not_trigger(self, store, monkeypatch):
        monkeypatch.setenv("MINICPM_EMBEDDING_API_KEY", "test-key")
        store.add_event(title="T", content="c", weight=800)
        loader.reset_session()

        async def fake_embed(text):
            return [1.0, 0.0]  # identical for everything → sim = 1.0 ≥ 0.65? no.

        # Make the msg vector orthogonal to the event vector.
        async def fake_embed_orth(text):
            return [0.0, 1.0] if "T" not in text else [1.0, 0.0]

        monkeypatch.setattr(rs, "_embed_text", fake_embed_orth)
        hits = await rs.find_resonance(store, "完全不相关的内容")
        assert hits == []  # cosine 0 < 0.65


class TestBuildResonanceBlock:
    def test_felt_language_and_time(self, seeded_store):
        evt = seeded_store.get_all_events()[0]
        block = rs.build_resonance_block([evt])
        assert "【你突然觉得这和现在有关】" in block
        assert "个月前的事" in block or "很久以前的事" in block
        assert evt["title"] in block
