"""E4: emotion index — model picks a tag, the system moves the index,
temperature follows.

Locks down:
  - per-tag deltas (calm=0, happy +0.1, sad -0.1, …)
  - clamping to [-1, 1]
  - persistence roundtrip through mood.json + legacy-file fallback
  - modulate_temperature mapping (happy → more divergent, sad → subdued)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.memory.mood import (
    EMOTION_INDEX_DELTAS,
    MoodStore,
    index_mood_phrase,
)
from gateway.server import modulate_temperature


@pytest.fixture
def store(tmp_path: Path) -> MoodStore:
    s = MoodStore()
    s.load_from_disk(tmp_path)
    return s


class TestDeltas:
    def test_calibration(self):
        """平静=0, 开心+0.1 — user's design anchor."""
        assert EMOTION_INDEX_DELTAS["happy"] == pytest.approx(0.10)
        assert EMOTION_INDEX_DELTAS["sad"] == pytest.approx(-0.10)
        assert EMOTION_INDEX_DELTAS["neutral"] == pytest.approx(-0.05)
        assert EMOTION_INDEX_DELTAS["excited"] > EMOTION_INDEX_DELTAS["happy"]
        assert EMOTION_INDEX_DELTAS["mad"] < EMOTION_INDEX_DELTAS["sad"]

    def test_all_known_tags_have_deltas(self):
        for tag in ("happy", "curious", "sad", "excited", "mad", "neutral", "scared"):
            assert tag in EMOTION_INDEX_DELTAS


class TestApplyEmotionTag:
    def test_happy_moves_up(self, store):
        assert store.apply_emotion_tag("happy") == pytest.approx(0.10)
        assert store.emotion_index == pytest.approx(0.10)

    def test_sad_moves_down(self, store):
        assert store.apply_emotion_tag("sad") == pytest.approx(-0.10)

    def test_case_insensitive(self, store):
        assert store.apply_emotion_tag("HAPPY") == pytest.approx(0.10)

    def test_unknown_tag_idempotent(self, store):
        assert store.apply_emotion_tag("ecstatic!!") == 0.0
        assert store.emotion_index == 0.0

    def test_clamp_at_max(self, store):
        for _ in range(30):
            store.apply_emotion_tag("happy")
        assert store.emotion_index == 1.0

    def test_clamp_at_min(self, store):
        for _ in range(30):
            store.apply_emotion_tag("mad")
        assert store.emotion_index == -1.0

    def test_neutral_drifts_back_to_calm(self, store):
        store.apply_emotion_tag("happy")
        assert store.emotion_index == pytest.approx(0.10)
        for _ in range(10):
            store.apply_emotion_tag("neutral")
        assert store.emotion_index == pytest.approx(-0.40)  # 0.10 - 10×0.05


class TestPersistence:
    def test_roundtrip(self, store, tmp_path: Path):
        store.apply_emotion_tag("happy")
        store.apply_emotion_tag("excited")
        assert store.emotion_index == pytest.approx(0.25)
        s2 = MoodStore()
        s2.load_from_disk(tmp_path)
        assert s2.emotion_index == pytest.approx(0.25)

    def test_legacy_file_without_index(self, tmp_path: Path):
        # A pre-index mood.json must load with index 0.0 (data.get fallback).
        (tmp_path / "mood.json").write_text(
            json.dumps({"current_mood": "开心", "intensity": 60}), encoding="utf-8"
        )
        s = MoodStore()
        s.load_from_disk(tmp_path)
        assert s.emotion_index == 0.0
        assert s.current_mood == "开心"


class TestMoodPhrase:
    def test_phrases(self):
        assert index_mood_phrase(0.5) == "高涨"
        assert index_mood_phrase(0.2) == "轻快"
        assert index_mood_phrase(0.0) == "平静"
        assert index_mood_phrase(-0.3) == "低沉"
        assert index_mood_phrase(-0.9) == "低落"


class TestModulateTemperature:
    def test_calm_keeps_base(self):
        assert modulate_temperature(0.6, 0.0) == pytest.approx(0.6)

    def test_happy_more_divergent(self):
        assert modulate_temperature(0.6, 0.3) == pytest.approx(0.69)
        assert modulate_temperature(0.6, 1.0) == pytest.approx(0.9)

    def test_sad_more_subdued(self):
        assert modulate_temperature(0.6, -0.5) == pytest.approx(0.45)
        assert modulate_temperature(0.6, -1.0) == pytest.approx(0.3)

    def test_clamped(self):
        assert modulate_temperature(0.6, 10.0) == 1.5
        assert modulate_temperature(0.6, -10.0) == 0.2
        assert modulate_temperature(0.05, 0.0) == 0.2  # base below floor
        assert modulate_temperature(2.0, 0.0) == 1.5  # base above cap

    def test_nonzero_base(self):
        assert modulate_temperature(0.8, 0.5) == pytest.approx(0.95)
