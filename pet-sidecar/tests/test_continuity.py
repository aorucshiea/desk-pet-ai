"""Continuity of self (持续自我存在) tests.

A body switch (theme switch) is NOT reincarnation — the pet is one
continuously-existing self. The switch is narrated into the memory
stream with continuity framing: the outgoing appearance notes the
change, the new one opens with "same me, new look", and the
body-change line is kept by the system's subconscious. The word
"转世" must not exist anywhere in this subsystem.
"""

from __future__ import annotations

import json
from pathlib import Path

from gateway.memory import continuity as cont
from gateway.memory_context import MemoryContext

_FORBIDDEN = ("转世", "来路单")


def _events_in(d: Path) -> list[dict]:
    p = d / "events.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8")).get("events", [])


def test_first_boot_no_history(tmp_path):
    ctx = MemoryContext(tmp_path)
    ctx.switch(None)
    assert cont.get_current_note() == ""
    assert cont.load_records(tmp_path) == []


def test_switch_records_body_change(tmp_path):
    ctx = MemoryContext(tmp_path)
    ctx.switch(None)
    ctx.switch("calico")

    records = cont.load_records(tmp_path)
    assert len(records) == 1
    assert records[0]["from"] == "default"
    assert records[0]["to"] == "calico"

    note = cont.get_current_note()
    assert "你一直是你" in note
    assert "default" in note and "calico" in note
    for word in _FORBIDDEN:
        assert word not in note

    # Same-appearance switch is a no-op, not a body change.
    ctx.switch("calico")
    assert len(cont.load_records(tmp_path)) == 1


def test_switch_events_use_continuity_framing(tmp_path):
    ctx = MemoryContext(tmp_path)
    ctx.switch(None)
    ctx.switch("calico")

    old_events = _events_in(tmp_path)
    assert any(e["title"] == "换下旧形象" for e in old_events)
    new_dir = tmp_path / "themes" / "calico"
    new_events = _events_in(new_dir)
    assert any(e["title"] == "换上新形象" for e in new_events)
    for e in old_events + new_events:
        for word in _FORBIDDEN:
            assert word not in e.get("title", "")
            assert word not in e.get("content", "")

    # Switching back: the line keeps growing.
    ctx.switch("default")
    assert cont.get_current_note().count("换到") == 2


def test_legacy_file_migrated(tmp_path):
    legacy = tmp_path / "reincarnations.json"
    legacy.write_text(json.dumps({
        "version": 1,
        "records": [{"at": "2026-08-30T10:00:00+00:00", "from": "default", "to": "calico"}],
    }, ensure_ascii=False), encoding="utf-8")
    records = cont.load_records(tmp_path)
    assert len(records) == 1
    # First write migrates to the new file name and removes the legacy one.
    cont.record(tmp_path, "calico", "default")
    assert (tmp_path / "body-changes.json").exists()
    assert not legacy.exists()


def test_build_note_empty_and_cap():
    assert cont.build_note([]) == ""
    records = [
        {"at": f"2026-08-{d:02d}T10:00:00+00:00", "from": f"t{d}", "to": f"t{d + 1}"}
        for d in range(1, 9)
    ]
    note = cont.build_note(records)
    assert note.count("换到") == cont._NOTE_MAX_ITEMS
    assert "t8" in note and "t1" not in note


def test_module_carries_no_reincarnation_language():
    src = Path(cont.__file__).read_text(encoding="utf-8")
    for word in _FORBIDDEN:
        assert word not in src
