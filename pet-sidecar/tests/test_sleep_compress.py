"""sleep_compress — bare-model day compression + persistence.

The compressor is deliberately persona-free: one instruction in, JSON out.
These tests cover the pure parsing/clamping paths and the two append targets
(EventStore + keywords.json) without touching a real provider.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from gateway.memory import sleep_compress


class FakeProvider:
    def __init__(self, text: str):
        self.text = text

    async def chat(self, messages, **kwargs):
        if self.text:
            yield {"type": "delta", "content": self.text}
        yield {"type": "end"}


class FakeEventStore:
    def __init__(self) -> None:
        self.events = []

    def add_event(self, **kwargs):
        self.events.append(kwargs)


def _write_day(root: Path, day: str, turns) -> None:
    log_dir = root / "conversations"
    log_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"t": f"{day}T12:00:00", "role": role, "content": text}, ensure_ascii=False)
        for role, text in turns
    ]
    (log_dir / f"{day}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_extract_json_plain_and_fenced():
    assert sleep_compress._extract_json('{"a": 1}') == {"a": 1}
    assert sleep_compress._extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert sleep_compress._extract_json("not json") is None


def test_compress_day_reads_the_days_log(tmp_path: Path):
    _write_day(tmp_path, "2026-10-19", [("user", "今天好累"), ("assistant", "早点休息")])
    provider = FakeProvider(
        '{"memories":[{"title":"深夜加班","content":"用户说累","weight":420}],'
        '"keywords":["加班","累"]}'
    )

    result = asyncio.run(sleep_compress.compress_day(provider, "2026-10-19", memory_dir=tmp_path))

    assert result["status"] == "ok"
    assert result["memories"][0]["title"] == "深夜加班"
    assert result["keywords"] == ["加班", "累"]


def test_compress_day_empty_log(tmp_path: Path):
    result = asyncio.run(
        sleep_compress.compress_day(FakeProvider("{}"), "2026-10-19", memory_dir=tmp_path)
    )
    assert result["status"] == "empty"


def test_compress_day_rejects_traversal_day(tmp_path: Path):
    result = asyncio.run(
        sleep_compress.compress_day(FakeProvider("{}"), "../../etc/passwd", memory_dir=tmp_path)
    )
    assert result["status"] == "error"
    assert "invalid day" in result["message"]


def test_compress_day_unparseable_output(tmp_path: Path):
    _write_day(tmp_path, "2026-10-19", [("user", "hello")])
    result = asyncio.run(
        sleep_compress.compress_day(FakeProvider("I cannot compress this"), "2026-10-19", memory_dir=tmp_path)
    )
    assert result["status"] == "error"
    assert "unparseable" in result["message"]


def test_persist_clamps_weights_and_writes_keywords(tmp_path: Path):
    store = FakeEventStore()
    result = sleep_compress.persist(
        [
            {"title": "零", "content": "下限", "weight": -5},
            {"title": "上限", "content": "超界", "weight": 5000},
            {"title": "坏值", "content": "非数字", "weight": "abc"},
            {"title": "", "content": "无标题", "weight": 100},
        ],
        ["a", "b"],
        store,
        memory_dir=tmp_path,
        day="2026-10-19",
    )

    assert result["events_written"] == 3
    assert [e["weight"] for e in store.events] == [1, 999, 300]
    data = json.loads((tmp_path / "keywords.json").read_text(encoding="utf-8"))
    assert data["keywords"] == ["a", "b"]
    assert data["day"] == "2026-10-19"


def test_persist_merges_keywords_new_first(tmp_path: Path):
    (tmp_path / "keywords.json").write_text(
        json.dumps({"keywords": ["old", "a"]}), encoding="utf-8"
    )
    sleep_compress.persist([], ["new", "a"], FakeEventStore(), memory_dir=tmp_path)

    data = json.loads((tmp_path / "keywords.json").read_text(encoding="utf-8"))
    # new keywords first, one copy of each, old survivors appended
    assert data["keywords"] == ["new", "a", "old"]


def test_persist_caps_keywords_at_24(tmp_path: Path):
    sleep_compress.persist(
        [], [f"k{i}" for i in range(40)], FakeEventStore(), memory_dir=tmp_path
    )

    data = json.loads((tmp_path / "keywords.json").read_text(encoding="utf-8"))
    assert len(data["keywords"]) == 24
    assert data["keywords"][0] == "k0"
    assert data["keywords"][-1] == "k23"
