"""conversation_log — the raw daily log that sleep compression reads.

These pin the two regressions fixed on 2026-10-19:
  * the fallback root must never be a literal "%APPDATA%" on POSIX;
  * the day used in a path must be a real YYYY-MM-DD (it comes from an
    HTTP body in /api/compress).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from gateway.memory import conversation_log


def test_append_read_and_format(tmp_path: Path):
    assert conversation_log.append_turn("user", "你好", memory_dir=tmp_path)
    assert conversation_log.append_turn("assistant", "你好呀", memory_dir=tmp_path)

    day = date.today().isoformat()
    turns = conversation_log.read_day(day, memory_dir=tmp_path)
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["content"] == "你好"

    text = conversation_log.format_day(day, memory_dir=tmp_path)
    assert "用户：你好" in text
    assert "你：你好呀" in text


def test_blank_turn_is_rejected(tmp_path: Path):
    assert not conversation_log.append_turn("user", "   \n", memory_dir=tmp_path)
    assert conversation_log.read_day(date.today().isoformat(), memory_dir=tmp_path) == []


def test_long_turn_is_truncated(tmp_path: Path):
    conversation_log.append_turn("user", "x" * (conversation_log.MAX_CHARS + 50), memory_dir=tmp_path)
    turns = conversation_log.read_day(date.today().isoformat(), memory_dir=tmp_path)
    assert len(turns[0]["content"]) == conversation_log.MAX_CHARS


def test_invalid_day_never_becomes_a_path(tmp_path: Path):
    # /api/compress takes {"day": "..."} from the body.
    assert conversation_log.read_day("../../etc/passwd", memory_dir=tmp_path) == []
    assert conversation_log.format_day("2026-13-99", memory_dir=tmp_path) == ""
    assert not conversation_log.valid_day("2026-10-19/../../x")
    assert conversation_log.valid_day("2026-10-19")


def test_prune_keeps_newest_files(tmp_path: Path):
    log_dir = tmp_path / "conversations"
    log_dir.mkdir(parents=True)
    for day in ("2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"):
        (log_dir / f"{day}.jsonl").write_text(
            json.dumps({"role": "user", "content": day}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    assert conversation_log.prune(2, memory_dir=tmp_path) == 2
    assert conversation_log.available_days(memory_dir=tmp_path) == ["2026-10-17", "2026-10-18"]


def test_default_root_respects_pet_memory_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PET_MEMORY_DIR", str(tmp_path / "custom"))
    assert conversation_log.default_memory_dir() == tmp_path / "custom"


def test_default_root_is_platform_aware(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PET_MEMORY_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    root = conversation_log.default_memory_dir()
    assert "%APPDATA%" not in str(root)
    assert root.name == "memories"
