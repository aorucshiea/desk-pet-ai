"""Unit tests for LingLing mood system."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from gateway.memory.mood import MoodStore, MOOD_PARAMS, MOOD_TO_EMOTION, build_mood_assessment_prompt


class TestMoodStore:
    @pytest.fixture
    def fresh_mood(self):
        with tempfile.TemporaryDirectory() as td:
            store = MoodStore()
            store.load_from_disk(Path(td))
            yield store

    def test_default_mood_is_calm(self, fresh_mood):
        assert fresh_mood.current_mood == "平静"
        assert fresh_mood.intensity == 40

    def test_update_mood_changes_state(self, fresh_mood):
        fresh_mood.update_mood(
            mood="开心", intensity=80, reason="用户说喜欢我", changed=True,
        )
        assert fresh_mood.current_mood == "开心"
        assert fresh_mood.intensity == 80
        assert fresh_mood.reason == "用户说喜欢我"

    def test_update_mood_without_change_keeps_since(self, fresh_mood):
        old_since = fresh_mood.since
        fresh_mood.update_mood(
            mood="平静", intensity=50, reason="还是一样", changed=False,
        )
        assert fresh_mood.current_mood == "平静"
        # Since may change because intensity changed
        assert fresh_mood.intensity == 50

    def test_update_mood_clamps_intensity(self, fresh_mood):
        fresh_mood.update_mood(mood="生气", intensity=999, reason="rage", changed=True)
        assert fresh_mood.intensity == 100
        fresh_mood.update_mood(mood="难过", intensity=-50, reason="nil", changed=True)
        assert fresh_mood.intensity == 0

    def test_unknown_mood_falls_back_to_calm(self, fresh_mood):
        fresh_mood.update_mood(mood="不存在的心情", intensity=50, reason="?", changed=True)
        assert fresh_mood.current_mood == "平静"

    def test_get_params_returns_dict(self, fresh_mood):
        params = fresh_mood.get_params()
        assert "color" in params
        assert "speed" in params
        assert "glow" in params

        fresh_mood.update_mood(mood="开心", intensity=80, reason="yay", changed=True)
        params = fresh_mood.get_params()
        assert params["bounce"] is True

    def test_get_emotion_tag_maps_correctly(self, fresh_mood):
        assert fresh_mood.get_emotion_tag() == "neutral"
        fresh_mood.update_mood(mood="开心", intensity=70, reason="", changed=True)
        assert fresh_mood.get_emotion_tag() == "happy"

    def test_format_for_system_prompt(self, fresh_mood):
        text = fresh_mood.format_for_system_prompt()
        assert "你此刻的心情" in text
        assert "平静" in text

    def test_persistence_roundtrip(self, fresh_mood):
        fresh_mood.update_mood(mood="难过", intensity=20, reason="没人理我", changed=True)
        mood2 = MoodStore()
        mood2.load_from_disk(Path(fresh_mood._memory_dir))
        assert mood2.current_mood == "难过"
        assert mood2.intensity == 20

    def test_history_recorded_on_change(self, fresh_mood):
        fresh_mood.update_mood(mood="开心", intensity=70, reason="a", changed=True)
        fresh_mood.update_mood(mood="难过", intensity=30, reason="b", changed=True)
        assert len(fresh_mood._history) >= 2


class TestMoodHelpers:
    def test_mood_params_all_have_required_keys(self):
        for mood, params in MOOD_PARAMS.items():
            assert "color" in params
            assert "speed" in params
            assert "glow" in params

    def test_emotion_mappings_are_symmetric(self):
        for mood in MOOD_PARAMS:
            tag = MOOD_TO_EMOTION.get(mood)
            assert tag is not None, f"Mood '{mood}' has no emotion tag mapping"

    def test_build_assessment_prompt_includes_mood(self):
        prompt = build_mood_assessment_prompt("开心", 70, "用户陪我聊天", "2026-01-01T00:00")
        assert "开心" in prompt
        assert "70" in prompt
        assert "用户陪我聊天" in prompt
        assert "events" in prompt
        assert "mood" in prompt
