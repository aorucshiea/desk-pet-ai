from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gateway.memory import sleep_schedule


def _write_day(root: Path, day: str, count: int) -> None:
    directory = root / "conversations"
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"role": "user" if i % 2 == 0 else "assistant", "content": f"消息{i}"})
        for i in range(count)
    ]
    (directory / f"{day}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_candidates_are_past_uncompressed_days_with_enough_turns(tmp_path):
    _write_day(tmp_path, "2026-10-17", 5)
    _write_day(tmp_path, "2026-10-18", 3)
    _write_day(tmp_path, "2026-10-19", 6)
    _write_day(tmp_path, "2026-10-20", 6)
    state = {"compressed_days": ["2026-10-17"]}

    assert sleep_schedule.candidate_days(
        tmp_path, state=state, min_turns=4, today="2026-10-20",
    ) == ["2026-10-19"]
    assert sleep_schedule.candidate_days(
        tmp_path, state={}, min_turns=4, today="2026-10-20",
    ) == ["2026-10-17", "2026-10-19"]


def test_idle_and_candidate_gate(tmp_path):
    _write_day(tmp_path, "2026-10-18", 4)
    now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)

    due, reason = sleep_schedule.should_run(tmp_path, None, now=now)
    assert not due and reason == "never_talked"
    due, reason = sleep_schedule.should_run(
        tmp_path, now - timedelta(minutes=30), idle_seconds=7200, now=now,
    )
    assert not due and reason == "not_idle"
    due, day = sleep_schedule.should_run(
        tmp_path, now - timedelta(hours=3), idle_seconds=7200, min_turns=4, now=now,
    )
    assert due and day == "2026-10-18"
    due, reason = sleep_schedule.should_run(
        tmp_path, now - timedelta(hours=3), state={"compressed_days": ["2026-10-18"]}, now=now,
    )
    assert not due and reason == "no_candidates"


def test_state_round_trip_and_success_marker(tmp_path):
    assert sleep_schedule.load_state(tmp_path) == {}
    (tmp_path / sleep_schedule.STATE_FILE).write_text("broken", encoding="utf-8")
    assert sleep_schedule.load_state(tmp_path) == {}

    now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    state = sleep_schedule.mark_completed(tmp_path, "2026-10-18", {"events_written": 2}, now=now)
    assert state["compressed_days"] == ["2026-10-18"]
    assert state["last_run_at"] == now.isoformat()
    assert state["last_result"]["status"] == "ok"
    assert state["last_result"]["events_written"] == 2

    loaded = sleep_schedule.load_state(tmp_path)
    assert loaded == state
    sleep_schedule.mark_completed(tmp_path, "2026-10-18", now=now)
    assert sleep_schedule.load_state(tmp_path)["compressed_days"] == ["2026-10-18"]
