"""P1: event structure v2 — emotion flow, type, resolved, core.

Locks down:
  - aggregate_emotion derives the event-level emotion from per-sentence
    （情绪） tags (model never writes a separate emotion field)
  - add_event v2 fields with safe defaults
  - content cap so top-5 full loads can't blow the context
  - backward compat: v1 events (no v2 fields) normalize cleanly
"""

from __future__ import annotations

import pytest

from gateway.memory.events import EventStore, aggregate_emotion


class TestAggregateEmotion:
    def test_most_frequent_emotion_wins(self):
        content = (
            "今天用户问我星期几（平静），我说星期二（平淡），"
            "反问用户咋连星期几都不知道（疑惑），他又问了一次（疑惑）。"
        )
        assert aggregate_emotion(content) == "疑惑"

    def test_chinese_and_english_parens(self):
        assert aggregate_emotion("他说好累(疲惫)，我说那早点睡(难过)") == "疲惫"

    def test_unknown_words_ignored(self):
        content = "他说好累（超级累），我回（随便啦）"
        assert aggregate_emotion(content) == ""

    def test_no_tags_returns_empty(self):
        assert aggregate_emotion("普通的没有标注的句子") == ""

    def test_empty_content(self):
        assert aggregate_emotion("") == ""
        assert aggregate_emotion(None) == ""


class TestAddEventV2:
    def test_full_fields(self, tmp_path):
        store = EventStore()
        store.load_from_disk(tmp_path)
        evt = store.add_event(
            title="加班夜",
            content="用户加班到两点（疲惫），我说该睡了（担心→难过）",
            weight=600,
            type_="experience",
            resolved=False,
            conversation_tokens=200,
        )
        assert evt["emotion"] == "疲惫"  # derived from flow
        assert evt["type"] == "experience"
        assert evt["resolved"] is False
        assert evt["core"] is False
        assert evt["conversation_tokens"] == 200

    def test_explicit_emotion_overrides_flow(self, tmp_path):
        store = EventStore()
        store.load_from_disk(tmp_path)
        evt = store.add_event(
            title="T", content="没有标注的句子", weight=100, emotion="开心"
        )
        assert evt["emotion"] == "开心"

    def test_defaults(self, tmp_path):
        store = EventStore()
        store.load_from_disk(tmp_path)
        evt = store.add_event(title="T", content="c", weight=100)
        assert evt["emotion"] == ""
        assert evt["type"] == "experience"
        assert evt["resolved"] is True
        assert evt["core"] is False
        assert evt["conversation_tokens"] == 0

    def test_bad_type_falls_back_to_experience(self, tmp_path):
        store = EventStore()
        store.load_from_disk(tmp_path)
        evt = store.add_event(title="T", content="c", weight=100, type_="gossip")
        assert evt["type"] == "experience"

    def test_knowledge_type(self, tmp_path):
        store = EventStore()
        store.load_from_disk(tmp_path)
        evt = store.add_event(title="新信息", content="用户住在上海（平静）", weight=300, type_="knowledge")
        assert evt["type"] == "knowledge"

    def test_content_capped(self, tmp_path):
        store = EventStore()
        store.load_from_disk(tmp_path)
        long_content = "字" * 2000
        evt = store.add_event(title="T", content=long_content, weight=100)
        assert len(evt["content"]) <= 500

    def test_title_capped_15(self, tmp_path):
        store = EventStore()
        store.load_from_disk(tmp_path)
        evt = store.add_event(title="很长很长很长很长很长的标题", content="c", weight=100)
        assert len(evt["title"]) <= 15


class TestBackwardCompat:
    def test_v1_event_normalizes(self, tmp_path):
        """A v1 event dict (no v2 fields) loaded from disk gets safe
        defaults via _normalize_event."""
        store = EventStore()
        store.load_from_disk(tmp_path)
        store.add_event(title="v1", content="老数据（难过）", weight=100)
        raw = store.get_all_events()[0]
        # Strip v2 fields to simulate a v1-era file, then reload.
        for key in ("emotion", "type", "resolved", "core", "conversation_tokens"):
            raw.pop(key)
        store.save()
        store2 = EventStore()
        store2.load_from_disk(tmp_path)
        evt = store2.get_all_events()[0]
        assert evt["emotion"] == "难过"  # derived from legacy content
        assert evt["type"] == "experience"
        assert evt["resolved"] is True
        assert evt["core"] is False
        assert evt["conversation_tokens"] == 0
