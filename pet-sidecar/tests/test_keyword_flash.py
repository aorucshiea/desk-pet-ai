"""Keyword-triggered flash — the lowest-probability tier of recall."""

from __future__ import annotations

import json
from pathlib import Path

from gateway.memory.ephemeral import EphemeralMemory
from gateway.memory.keyword_flash import load_keywords, maybe_flash


class FakeEvents:
    def search_by_keyword(self, keyword: str, limit: int = 3):
        if keyword == "加班":
            return [{"title": "深夜加班", "content": "用户 22 点后说累"}]
        return []


class Rng:
    def __init__(self, value: float = 0.0):
        self.value = value

    def random(self) -> float:
        return self.value

    def choice(self, seq):
        return seq[0]


def _keywords(tmp_path: Path, words) -> None:
    (tmp_path / "keywords.json").write_text(
        json.dumps({"keywords": words}, ensure_ascii=False), encoding="utf-8"
    )


def test_loads_keywords(tmp_path: Path):
    _keywords(tmp_path, ["加班", "累"])
    assert load_keywords(tmp_path) == ["加班", "累"]


def test_hit_flashes_a_related_memory(tmp_path: Path):
    _keywords(tmp_path, ["加班", "累"])
    eph = EphemeralMemory(tmp_path)

    flash = maybe_flash("今天又加班了", tmp_path, FakeEvents(), eph, rng=Rng(0.0), probability=0.05)

    assert flash is not None
    assert "加班" in flash
    assert "深夜加班" in flash
    assert eph.flashes and eph.flashes[0]["kind"] == "keyword"


def test_no_keyword_hit_is_silent(tmp_path: Path):
    _keywords(tmp_path, ["加班"])
    eph = EphemeralMemory(tmp_path)
    assert maybe_flash("今天天气不错", tmp_path, FakeEvents(), eph, rng=Rng(0.0)) is None
    assert eph.flashes == []


def test_low_probability_skips(tmp_path: Path):
    _keywords(tmp_path, ["加班"])
    eph = EphemeralMemory(tmp_path)
    assert maybe_flash("今天又加班了", tmp_path, FakeEvents(), eph, rng=Rng(0.9)) is None


def test_same_flash_is_not_repeated(tmp_path: Path):
    _keywords(tmp_path, ["加班"])
    eph = EphemeralMemory(tmp_path)

    first = maybe_flash("今天又加班了", tmp_path, FakeEvents(), eph, rng=Rng(0.0))
    second = maybe_flash("今天又加班了", tmp_path, FakeEvents(), eph, rng=Rng(0.0))

    assert first is not None
    assert second is None
    assert len(eph.flashes) == 1
