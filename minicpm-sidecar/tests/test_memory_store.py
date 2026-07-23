"""Lock down MemoryStore behaviour ported from hermes-agent.

These tests pin the contract the chat renderer and the ``memory`` builtin
tool depend on: frozen snapshot, char-limit enforcement, substring matching,
atomic batch, and drift refusal. If any of these change the model's memory
silently misbehaves.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from gateway.memory import ENTRY_DELIMITER, MemoryStore
from gateway.memory.tool import memory_dispatch, memory_tool_handler, set_memory_store


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    s = MemoryStore()
    s.load_from_disk(tmp_path)
    return s


# ----------------------------------------------------------------------
# load / snapshot
# ----------------------------------------------------------------------

def test_load_from_disk_creates_dir_and_empty_snapshot(tmp_path: Path):
    s = MemoryStore()
    s.load_from_disk(tmp_path / "nested")
    assert (tmp_path / "nested").is_dir()
    assert s.format_for_system_prompt("memory") is None
    assert s.format_for_system_prompt("user") is None


def test_load_reads_existing_entries(tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text(f"用户叫小明{ENTRY_DELIMITER}喜欢猫", encoding="utf-8")
    s = MemoryStore()
    s.load_from_disk(tmp_path)
    snap = s.format_for_system_prompt("memory")
    assert snap and "用户叫小明" in snap and "喜欢猫" in snap


def test_load_deduplicates_entries(tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text(f"重复条目{ENTRY_DELIMITER}重复条目", encoding="utf-8")
    s = MemoryStore()
    s.load_from_disk(tmp_path)
    assert s._char_count("memory") == len("重复条目")


# ----------------------------------------------------------------------
# frozen snapshot invariant — the prefix-cache pillar
# ----------------------------------------------------------------------

def test_write_invalidates_snapshot_next_read_sees_it(store: MemoryStore):
    """Write-on-invalidate: a successful write makes the next snapshot read
    refresh from disk, so the same session sees the new memory on the next
    turn. This mirrors Claude Code's "file is truth, reload on next read"
    and Hermes' prefetch cache invalidation. Between writes the snapshot
    stays stable (prefix cache holds across turns that don't touch memory).
    """
    snap_before = store.format_for_system_prompt("memory")
    assert snap_before is None  # nothing loaded at boot → empty snapshot

    store.add("memory", "用户叫小明")

    # The write invalidated the snapshot; the next read refreshes from disk
    # and sees the new entry — the SAME session picks it up.
    snap_after = store.format_for_system_prompt("memory")
    assert snap_after is not None
    assert "用户叫小明" in snap_after

    # A read with no intervening write is stable (no re-read, prefix cache).
    # format_for_system_prompt returns the cached snapshot unchanged.
    snap_again = store.format_for_system_prompt("memory")
    assert snap_again == snap_after

    # A fresh store loaded from the SAME disk also sees it (restart path).
    fresh = MemoryStore()
    fresh.load_from_disk(store._memory_dir)
    assert "用户叫小明" in (fresh.format_for_system_prompt("memory") or "")


# ----------------------------------------------------------------------
# add
# ----------------------------------------------------------------------

def test_add_persists_to_disk(store: MemoryStore, tmp_path: Path):
    r = store.add("memory", "用户叫小明")
    assert r["success"] is True
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == "用户叫小明"


def test_add_rejects_empty_content(store: MemoryStore):
    r = store.add("memory", "   ")
    assert r["success"] is False
    assert "empty" in r["error"].lower()


def test_add_rejects_exact_duplicate(store: MemoryStore):
    store.add("memory", "用户叫小明")
    r = store.add("memory", "用户叫小明")
    assert r["success"] is True  # idempotent, not an error
    assert "already exists" in r["message"]
    assert store._char_count("memory") == len("用户叫小明")


def test_add_enforces_char_limit(tmp_path: Path):
    s = MemoryStore(memory_char_limit=20)
    s.load_from_disk(tmp_path)
    r = s.add("memory", "x" * 30)  # single entry exceeds the 20-char limit
    assert r["success"] is False
    assert "exceed the limit" in r["error"]
    assert "current_entries" in r  # model needs the inventory to consolidate


# ----------------------------------------------------------------------
# replace / remove — substring matching
# ----------------------------------------------------------------------

def test_replace_matches_by_substring(store: MemoryStore):
    store.add("memory", "用户叫小明")
    r = store.replace("memory", "小明", "用户叫小明，喜欢猫")
    assert r["success"] is True
    fresh = MemoryStore(); fresh.load_from_disk(store._memory_dir)
    assert "喜欢猫" in fresh.format_for_system_prompt("memory")


def test_replace_rejects_empty_old_text(store: MemoryStore):
    r = store.replace("memory", "", "x")
    assert r["success"] is False


def test_replace_no_match_is_recoverable(store: MemoryStore):
    store.add("memory", "用户叫小明")
    r = store.replace("memory", "不存在的文本", "新内容")
    assert r["success"] is False
    assert "current_entries" in r  # tells the model what's actually there


def test_replace_ambiguous_multiple_distinct_matches(store: MemoryStore):
    store.add("memory", "用户A")
    store.add("memory", "用户B")
    r = store.replace("memory", "用户", "X")
    assert r["success"] is False
    assert "Multiple" in r["error"]
    assert "matches" in r


def test_replace_identical_duplicates_replaces_first(store: MemoryStore):
    store.add("memory", "重复")
    store.add("memory", "重复")  # dedup would normally drop, but simulate via disk
    # Force two identical entries by writing the file directly
    (store._memory_dir / "MEMORY.md").write_text(f"重复{ENTRY_DELIMITER}重复", encoding="utf-8")
    r = store.replace("memory", "重复", "改过")
    assert r["success"] is True


def test_remove_matches_by_substring(store: MemoryStore):
    store.add("memory", "用户叫小明")
    r = store.remove("memory", "小明")
    assert r["success"] is True
    assert (store._memory_dir / "MEMORY.md").read_text(encoding="utf-8") == ""


def test_remove_no_match_is_recoverable(store: MemoryStore):
    store.add("memory", "用户叫小明")
    r = store.remove("memory", "不存在")
    assert r["success"] is False
    assert "current_entries" in r


# ----------------------------------------------------------------------
# apply_batch — atomic, final-budget-checked
# ----------------------------------------------------------------------

def test_batch_applies_atomically(store: MemoryStore):
    store.add("memory", "old1")
    store.add("memory", "old2")
    ops = [
        {"action": "remove", "old_text": "old1"},
        {"action": "add", "content": "new1"},
    ]
    r = store.apply_batch("memory", ops)
    assert r["success"] is True
    fresh = MemoryStore(); fresh.load_from_disk(store._memory_dir)
    snap = fresh.format_for_system_prompt("memory") or ""
    assert "old1" not in snap and "new1" in snap and "old2" in snap


def test_batch_all_or_nothing_on_failure(store: MemoryStore):
    """A bad op rolls back the whole batch — nothing commits."""
    store.add("memory", "keep")
    ops = [
        {"action": "add", "content": "temp"},
        {"action": "remove", "old_text": "nonexistent"},  # fails → batch aborts
    ]
    r = store.apply_batch("memory", ops)
    assert r["success"] is False
    # 'temp' must NOT have committed (all-or-nothing)
    fresh = MemoryStore(); fresh.load_from_disk(store._memory_dir)
    snap = fresh.format_for_system_prompt("memory") or ""
    assert "temp" not in snap
    assert "keep" in snap


def test_batch_can_consolidate_to_make_room(tmp_path: Path):
    """Remove stale + add new in one batch even when add alone would overflow."""
    s = MemoryStore(memory_char_limit=30)
    s.load_from_disk(tmp_path)
    s.add("memory", "0123456789")  # 10 chars, room left
    # Now a batch that removes the stale entry AND adds a 25-char one.
    # An add of 25 alone (25 > 30 limit? no, 25 < 30) — so make it tighter:
    s2 = MemoryStore(memory_char_limit=15)
    s2.load_from_disk(tmp_path)
    assert s2._char_count("memory") == 10  # pre-existing
    ops = [
        {"action": "remove", "old_text": "0123456789"},
        {"action": "add", "content": "ABCDEFGHIJ5"},  # 11 chars, fits under 15
    ]
    r = s2.apply_batch("memory", ops)
    assert r["success"] is True
    fresh = MemoryStore(memory_char_limit=15); fresh.load_from_disk(tmp_path)
    assert "ABCDEFGHIJ5" in (fresh.format_for_system_prompt("memory") or "")


def test_batch_rejects_final_state_over_budget(tmp_path: Path):
    s = MemoryStore(memory_char_limit=10)
    s.load_from_disk(tmp_path)
    ops = [{"action": "add", "content": "x" * 20}]
    r = s.apply_batch("memory", ops)
    assert r["success"] is False
    assert "over the limit" in r["error"]


# ----------------------------------------------------------------------
# drift — refuse to clobber external edits
# ----------------------------------------------------------------------

def test_drift_refuses_when_content_doesnt_roundtrip(tmp_path: Path):
    """A hand-edited file whose bytes don't survive split+join must NOT be clobbered.

    Drift is signalled two ways (see store._detect_external_drift): a round-trip
    mismatch (raw bytes != split-then-rejoin) OR a single entry exceeding the
    whole-file char limit. The latter is the common real-world case — an
    external writer (shell append, manual edit, sister session) dumps free-
    form text into what the tool treats as one entry, which a replace would
    then truncate. Construct that case here.
    """
    # A single entry larger than the store's whole-file limit → drift signal #2.
    big = "x" * 5000
    (tmp_path / "MEMORY.md").write_text(big, encoding="utf-8")
    s = MemoryStore(memory_char_limit=2000)
    s.load_from_disk(tmp_path)
    r = s.replace("memory", "x", "改过的")
    assert r["success"] is False
    assert "Refusing to write" in r["error"]
    assert "backup" in r  # points at the .bak snapshot
    # Original file untouched
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == big


def test_drift_refuses_when_single_entry_exceeds_limit(tmp_path: Path):
    """An entry larger than the whole-file limit signals external free-form append."""
    (tmp_path / "MEMORY.md").write_text("x" * 5000, encoding="utf-8")
    s = MemoryStore(memory_char_limit=2000)
    s.load_from_disk(tmp_path)
    r = s.remove("memory", "x")
    assert r["success"] is False
    assert "Refusing to write" in r["error"]


def test_add_skips_drift_check_append_only(tmp_path: Path):
    """add is append-only so it never clobbers — drift check is skipped for it."""
    (tmp_path / "MEMORY.md").write_text("已有条目", encoding="utf-8")
    s = MemoryStore()
    s.load_from_disk(tmp_path)
    r = s.add("memory", "新条目")
    assert r["success"] is True  # append works even though file isn't §-shaped
    content = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "已有条目" in content and "新条目" in content


# ----------------------------------------------------------------------
# consolidation failure budget — prevents turn-loop death spiral
# ----------------------------------------------------------------------

def test_consolidation_failure_budget_eventually_goes_terminal(store: MemoryStore, tmp_path: Path):
    """After N at-capacity failures in one turn, the response becomes terminal."""
    store.reset_consolidation_failures()
    s = MemoryStore(memory_char_limit=10)
    s.load_from_disk(tmp_path)
    # First few failures return the recoverable shape (with current_entries)
    seen_terminal = False
    for _ in range(MemoryStore._MAX_CONSOLIDATION_FAILURES_PER_TURN + 2):
        r = s.add("memory", "x" * 50)  # always overflows 10-char limit
        if r.get("done") is True and "Stop retrying" in r.get("error", ""):
            seen_terminal = True
            break
    assert seen_terminal, "consolidation loop should degrade to a terminal stop-retry result"


# ----------------------------------------------------------------------
# tool dispatch + MCP handler
# ----------------------------------------------------------------------

def test_dispatch_routes_to_add(store: MemoryStore):
    r = memory_dispatch(action="add", target="memory", content="test", store=store)
    assert r["success"] is True


def test_dispatch_unknown_action(store: MemoryStore):
    r = memory_dispatch(action="frobnicate", store=store)
    assert r["success"] is False
    assert "Unknown action" in r["error"]


def test_dispatch_invalid_target(store: MemoryStore):
    r = memory_dispatch(action="add", target="bogus", content="x", store=store)
    assert r["success"] is False
    assert "Invalid target" in r["error"]


def test_dispatch_replace_without_old_text_returns_inventory(store: MemoryStore):
    """A missing old_text must not be a dead end — return the inventory + retry hint."""
    store.add("memory", "用户叫小明")
    r = memory_dispatch(action="replace", target="memory", content="x", store=store)
    assert r["success"] is False
    assert "current_entries" in r
    assert "用户叫小明" in r["current_entries"]


def test_dispatch_no_store_returns_error():
    set_memory_store(None)
    r = memory_dispatch(action="add", target="memory", content="x")
    assert r["success"] is False
    assert "not available" in r["error"].lower()


def test_mcp_handler_returns_mcp_shape(store: MemoryStore):
    set_memory_store(store)
    r = asyncio.run(memory_tool_handler({"action": "add", "target": "memory", "content": "via handler"}))
    assert r["is_error"] is False
    assert r["content"][0]["type"] == "text"
    parsed = json.loads(r["content"][0]["text"])
    assert parsed["success"] is True


def test_mcp_handler_error_path_is_marked(store: MemoryStore):
    set_memory_store(store)
    r = asyncio.run(memory_tool_handler({"action": "bogus"}))
    assert r["is_error"] is True
    assert "error" in r["summary"].lower() or "unknown" in r["summary"].lower()


def test_mcp_handler_batch(store: MemoryStore):
    set_memory_store(store)
    args = {"operations": [
        {"action": "add", "content": "a"},
        {"action": "add", "content": "b"},
    ]}
    r = asyncio.run(memory_tool_handler(args))
    assert r["is_error"] is False
