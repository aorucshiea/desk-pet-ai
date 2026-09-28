"""MemoryStore — bounded, file-backed, cross-session memory for the desk pet.

Ported from hermes-agent ``tools/memory_tool.py``. See ``__init__.py`` for
the design notes and the deliberate simplifications vs upstream.

The store is the single source of truth for what the pet "remembers" across
restarts. The gateway boots one store per process, loads the frozen snapshot
once, and exposes:

  - ``format_for_system_prompt(target)`` for the snapshot injected into the
    system prompt on every chat turn (frozen → prefix-cache safe).
  - ``add`` / ``replace`` / ``remove`` / ``apply_batch`` for the ``memory``
    builtin tool the model calls to write new facts.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..log_setup import get_logger

logger = get_logger()


# Entry separator. Multi-char + newline so it survives inside content that
# happens to contain a bare "§" (the strip/split keys on the full sentinel).
ENTRY_DELIMITER = "\n§\n"


class MemoryStore:
    """Bounded curated memory with file persistence. One instance per sidecar.

    Maintains two parallel states:
      - ``_system_prompt_snapshot``: frozen at load time, used for system
        prompt injection. Never mutated mid-session. Keeps prefix cache.
      - ``memory_entries`` / ``user_entries``: live state, mutated by tool
        calls, persisted to disk. Tool responses always reflect live state.
    """

    # After this many failed consolidation attempts (overflow / zero-match)
    # in ONE turn, stop telling the model to retry and return a terminal
    # "save skipped" result so a fragile write can't loop the turn to budget
    # exhaustion and suppress the user's reply.
    _MAX_CONSOLIDATION_FAILURES_PER_TURN = 3

    def __init__(
        self,
        memory_char_limit: int = 2200,
        user_char_limit: int = 1375,
    ) -> None:
        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        # Snapshot for system-prompt injection. Refreshed on boot AND whenever
        # a write lands (invalidate-on-write, mirroring Claude Code's "file is
        # truth, reload on next read" and Hermes' prefetch cache invalidation).
        # The snapshot stays stable BETWEEN writes so the provider prefix
        # cache holds across turns — but a write bumps it so the very next
        # turn sees the new memory, not a stale frozen copy.
        self._system_prompt_snapshot: Dict[str, str] = {"memory": "", "user": ""}
        self._snapshot_dirty: bool = False
        # Per-turn counter of failed at-capacity consolidation attempts; reset
        # at each turn boundary by reset_consolidation_failures().
        self._consolidation_failures = 0
        # Directory resolved by load_from_disk(); used by _path_for().
        self._memory_dir: Optional[Path] = None

    # ------------------------------------------------------------------
    # Turn lifecycle
    # ------------------------------------------------------------------

    def reset_consolidation_failures(self) -> None:
        """Reset the per-turn consolidation-failure counter (call at turn start)."""
        self._consolidation_failures = 0

    def _consolidation_failure(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Count a failed at-capacity consolidation and degrade gracefully.

        Under the per-turn cap, return ``response`` unchanged (it already
        tells the model how to self-correct + retry in this turn). Once the
        cap is exceeded, drop the retry instruction and return a TERMINAL
        result so the model stops looping memory calls and proceeds to
        answer — a failed memory side effect must never block the reply.
        """
        self._consolidation_failures += 1
        if self._consolidation_failures <= self._MAX_CONSOLIDATION_FAILURES_PER_TURN:
            return response
        return {
            "success": False,
            "done": True,
            "error": (
                f"Memory consolidation failed {self._consolidation_failures} times "
                "this turn. Stop retrying memory calls — leave memory unchanged for "
                "now and continue with your reply to the user. The fact can be saved "
                "in a later turn."
            ),
        }

    # ------------------------------------------------------------------
    # Load / snapshot
    # ------------------------------------------------------------------

    def load_from_disk(self, memory_dir: Path) -> None:
        """Load entries from MEMORY.md and USER.md, capture the snapshot.

        The snapshot is what enters the system prompt. It refreshes on boot
        AND whenever a write lands (invalidate-on-write, mirroring Claude
        Code's "file is truth, reload on next read" and Hermes' prefetch
        cache invalidation): the snapshot stays stable BETWEEN writes so the
        provider prefix cache holds across turns, but a write bumps it so
        the very next turn sees the new memory, not a stale frozen copy.
        """
        self._memory_dir = Path(memory_dir).expanduser()
        self._memory_dir.mkdir(parents=True, exist_ok=True)

        self.memory_entries = self._read_file(self._path_for("memory"))
        self.user_entries = self._read_file(self._path_for("user"))

        # Deduplicate (preserves order, keeps first occurrence).
        self.memory_entries = list(dict.fromkeys(self.memory_entries))
        self.user_entries = list(dict.fromkeys(self.user_entries))

        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", self.memory_entries),
            "user": self._render_block("user", self.user_entries),
        }
        self._snapshot_dirty = False

    def refresh_snapshot(self) -> None:
        """Re-read disk and refresh the snapshot (invalidate-on-write).

        Called after every successful write so the next system-prompt read
        sees the new memory. Cheap (two small file reads) and keeps the
        snapshot consistent with disk without re-running full load_from_disk.
        """
        if self._memory_dir is None:
            return
        self.memory_entries = self._read_file(self._path_for("memory"))
        self.user_entries = self._read_file(self._path_for("user"))
        self.memory_entries = list(dict.fromkeys(self.memory_entries))
        self.user_entries = list(dict.fromkeys(self.user_entries))
        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", self.memory_entries),
            "user": self._render_block("user", self.user_entries),
        }
        self._snapshot_dirty = False

    def format_for_system_prompt(self, target: str) -> Optional[str]:
        """Return the snapshot for system-prompt injection.

        Snapshot refreshes on boot and after every write (see
        ``refresh_snapshot``), so it reflects the latest disk state while
        still staying stable BETWEEN writes (prefix cache holds across
        turns that don't touch memory). Returns ``None`` if empty.
        """
        if self._snapshot_dirty:
            self.refresh_snapshot()
        block = self._system_prompt_snapshot.get(target, "")
        return block if block else None

    # ------------------------------------------------------------------
    # Mutations (the memory tool surface)
    # ------------------------------------------------------------------

    def add(self, target: str, content: str) -> Dict[str, Any]:
        """Append a new entry. Returns error if it would exceed the char limit."""
        content = (content or "").strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        # Reload from disk before mutating. add is append-only, so the drift
        # guard (which protects against clobbering un-roundtrippable content
        # on full-file rewrite) is skipped — appending never clobbers.
        self._reload_target(target, skip_drift=True)

        entries = self._entries_for(target)
        limit = self._char_limit(target)

        # Reject exact duplicates.
        if content in entries:
            return self._success_response(target, "Entry already exists (no duplicate added).")

        new_entries = entries + [content]
        new_total = len(ENTRY_DELIMITER.join(new_entries))
        if new_total > limit:
            current = self._char_count(target)
            return self._consolidation_failure({
                "success": False,
                "error": (
                    f"Memory at {current:,}/{limit:,} chars. "
                    f"Adding this entry ({len(content)} chars) would exceed the limit. "
                    f"Consolidate now: use 'replace' to merge overlapping entries into "
                    f"shorter ones or 'remove' stale entries (see current_entries "
                    f"below), then retry this add — all in this turn."
                ),
                "current_entries": entries,
                "usage": f"{current:,}/{limit:,}",
            })

        entries.append(content)
        self._set_entries(target, entries)
        self.save_to_disk(target)
        self._snapshot_dirty = True  # invalidate-on-write → next read refreshes

        return self._success_response(target, "Entry added.")

    def replace(self, target: str, old_text: str, new_content: str) -> Dict[str, Any]:
        """Find entry containing ``old_text`` substring, replace it with ``new_content``."""
        old_text = (old_text or "").strip()
        new_content = (new_content or "").strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {"success": False, "error": "new_content cannot be empty. Use 'remove' to delete entries."}

        # Reload + drift-check: replace rewrites the whole file, so we must
        # refuse if the on-disk shape wouldn't round-trip (would clobber an
        # external edit). bak is None on a clean reload.
        bak = self._reload_target(target)
        if bak is not None:
            return _drift_error(self._path_for(target), bak)

        entries = self._entries_for(target)
        matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

        if not matches:
            return self._consolidation_failure({
                "success": False,
                "error": f"No entry matched '{old_text}'. Check current_entries below and retry with the exact text of the entry you want to replace.",
                "current_entries": entries,
            })

        if len(matches) > 1:
            unique_texts = {e for _, e in matches}
            if len(unique_texts) > 1:
                previews = self._previews([e for _, e in matches])
                return {
                    "success": False,
                    "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                    "matches": previews,
                }
            # All identical — safe to replace just the first.

        idx = matches[0][0]
        limit = self._char_limit(target)

        test_entries = entries.copy()
        test_entries[idx] = new_content
        new_total = len(ENTRY_DELIMITER.join(test_entries))
        if new_total > limit:
            current = self._char_count(target)
            return self._consolidation_failure({
                "success": False,
                "error": (
                    f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                    f"Shorten the new content, or 'remove' other stale entries to make "
                    f"room (see current_entries below), then retry — all in this turn."
                ),
                "current_entries": entries,
                "usage": f"{current:,}/{limit:,}",
            })

        entries[idx] = new_content
        self._set_entries(target, entries)
        self.save_to_disk(target)
        self._snapshot_dirty = True  # invalidate-on-write → next read refreshes

        return self._success_response(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        """Remove the entry containing ``old_text`` substring."""
        old_text = (old_text or "").strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        bak = self._reload_target(target)
        if bak is not None:
            return _drift_error(self._path_for(target), bak)

        entries = self._entries_for(target)
        matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

        if not matches:
            return self._consolidation_failure({
                "success": False,
                "error": f"No entry matched '{old_text}'. Check current_entries below and retry with the exact text of the entry you want to remove.",
                "current_entries": entries,
            })

        if len(matches) > 1:
            unique_texts = {e for _, e in matches}
            if len(unique_texts) > 1:
                previews = self._previews([e for _, e in matches])
                return {
                    "success": False,
                    "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                    "matches": previews,
                }

        idx = matches[0][0]
        entries.pop(idx)
        self._set_entries(target, entries)
        self.save_to_disk(target)
        self._snapshot_dirty = True  # invalidate-on-write → next read refreshes

        return self._success_response(target, "Entry removed.")

    def apply_batch(self, target: str, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Apply a sequence of add/replace/remove ops atomically against the final budget.

        All-or-nothing: if any op is malformed, doesn't match, or the net
        result would exceed the char limit, NOTHING is written.
        """
        if not operations:
            return {"success": False, "error": "operations list is empty."}

        bak = self._reload_target(target)
        if bak is not None:
            return _drift_error(self._path_for(target), bak)

        working: List[str] = list(self._entries_for(target))
        limit = self._char_limit(target)

        for i, op in enumerate(operations):
            op = op or {}
            act = op.get("action")
            content = (op.get("content") or "").strip()
            old_text = (op.get("old_text") or "").strip()
            pos = f"Operation {i + 1} ({act or 'unknown'})"

            if act == "add":
                if not content:
                    return self._batch_error(target, f"{pos}: content is required.")
                if content in working:
                    continue  # idempotent — skip duplicate, don't fail the batch
                working.append(content)

            elif act == "replace":
                if not old_text:
                    return self._batch_error(target, f"{pos}: old_text is required.")
                if not content:
                    return self._batch_error(
                        target,
                        f"{pos}: content is required (use action='remove' to delete).",
                    )
                matches = [j for j, e in enumerate(working) if old_text in e]
                if not matches:
                    return self._batch_error(target, f"{pos}: no entry matched '{old_text}'.")
                if len({working[j] for j in matches}) > 1:
                    return self._batch_error(
                        target,
                        f"{pos}: '{old_text}' matched multiple distinct entries — be more specific.",
                    )
                working[matches[0]] = content

            elif act == "remove":
                if not old_text:
                    return self._batch_error(target, f"{pos}: old_text is required.")
                matches = [j for j, e in enumerate(working) if old_text in e]
                if not matches:
                    return self._batch_error(target, f"{pos}: no entry matched '{old_text}'.")
                if len({working[j] for j in matches}) > 1:
                    return self._batch_error(
                        target,
                        f"{pos}: '{old_text}' matched multiple distinct entries — be more specific.",
                    )
                working.pop(matches[0])

            else:
                return self._batch_error(
                    target,
                    f"{pos}: unknown action. Use add, replace, or remove.",
                )

        new_total = len(ENTRY_DELIMITER.join(working)) if working else 0
        if new_total > limit:
            current = self._char_count(target)
            return self._consolidation_failure({
                "success": False,
                "error": (
                    f"After applying all {len(operations)} operations, memory would be at "
                    f"{new_total:,}/{limit:,} chars — over the limit. Remove or shorten more "
                    f"entries in the same batch (see current_entries below), then retry."
                ),
                "current_entries": self._entries_for(target),
                "usage": f"{current:,}/{limit:,}",
            })

        self._set_entries(target, working)
        self.save_to_disk(target)
        self._snapshot_dirty = True  # invalidate-on-write → next read refreshes

        return self._success_response(target, f"Applied {len(operations)} operation(s).")

    def _batch_error(self, target: str, message: str) -> Dict[str, Any]:
        """Build a batch-abort error that reports live (uncommitted) state."""
        current = self._char_count(target)
        limit = self._char_limit(target)
        return self._consolidation_failure({
            "success": False,
            "error": message + " No operations were applied (batch is all-or-nothing).",
            "current_entries": self._entries_for(target),
            "usage": f"{current:,}/{limit:,}",
        })

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def save_to_disk(self, target: str) -> None:
        """Persist entries. Called after every mutation."""
        if self._memory_dir is None:
            raise RuntimeError("MemoryStore.save_to_disk called before load_from_disk")
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._write_file(self._path_for(target), self._entries_for(target))

    def _path_for(self, target: str) -> Path:
        if self._memory_dir is None:
            raise RuntimeError("MemoryStore used before load_from_disk")
        if target == "user":
            return self._memory_dir / "USER.md"
        return self._memory_dir / "MEMORY.md"

    def _entries_for(self, target: str) -> List[str]:
        return self.user_entries if target == "user" else self.memory_entries

    def _set_entries(self, target: str, entries: List[str]) -> None:
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

    def _char_limit(self, target: str) -> int:
        return self.user_char_limit if target == "user" else self.memory_char_limit

    def _reload_target(self, target: str, *, skip_drift: bool = False) -> Optional[str]:
        """Re-read entries from disk into live state. Called before mutating.

        Returns the backup-path string if external drift was detected (the
        on-disk file contains content that wouldn't round-trip through our
        parser, OR an entry larger than the whole-file char limit — a sign an
        external writer appended free-form content). When drift is detected
        the caller must abort the mutation: flushing would discard the
        un-roundtrippable content. Returns ``None`` on clean reload.

        ``skip_drift=True`` bypasses the round-trip check — used by ``add``
        (append-only never clobbers existing content).
        """
        path = self._path_for(target)
        bak = None if skip_drift else self._detect_external_drift(target)
        fresh = self._read_file(path)
        fresh = list(dict.fromkeys(fresh))  # dedupe
        self._set_entries(target, fresh)
        return bak

    def _detect_external_drift(self, target: str) -> Optional[str]:
        """Return a ``.bak`` path if on-disk content shows external drift.

        Two signals:
          1. Round-trip mismatch — re-parsing + re-serializing the file
             doesn't reproduce identical bytes (odd delimiters, hand-edits).
          2. Entry-size overflow — any single parsed entry exceeds the
             whole-file char limit (external writer appended free-form text
             the tool would treat as one entry and truncate on flush).
        """
        path = self._path_for(target)
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return None
        if not raw.strip():
            return None

        parsed = [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]
        roundtrip = ENTRY_DELIMITER.join(parsed)

        char_limit = self._char_limit(target)
        max_entry_len = max((len(e) for e in parsed), default=0)

        drift_detected = (raw.strip() != roundtrip) or (max_entry_len > char_limit)
        if not drift_detected:
            return None

        ts = int(time.time())
        bak_path = path.with_suffix(path.suffix + f".bak.{ts}")
        try:
            bak_path.write_text(raw, encoding="utf-8")
        except (OSError, IOError):
            return str(bak_path) + " (BACKUP FAILED — file unchanged on disk)"
        return str(bak_path)

    @staticmethod
    def _read_file(path: Path) -> List[str]:
        """Read a memory file and split into entries.

        No lock needed: ``_write_file`` uses atomic rename, so readers see
        either the previous complete file or the new one — never a partial.
        """
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return []
        if not raw.strip():
            return []
        # Split on the full sentinel (not bare "§") so an entry containing a
        # bare section sign survives.
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    @staticmethod
    def _write_file(path: Path, entries: List[str]) -> None:
        """Write entries using atomic temp-file + rename.

        Readers always see either the old complete file or the new one —
        never an empty/partial mid-write state.
        """
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), suffix=".tmp", prefix=".mem_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Snapshot rendering
    # ------------------------------------------------------------------

    @staticmethod
    def _previews(entries: List[str], width: int = 80) -> List[str]:
        """Truncated one-line previews of entries for error feedback."""
        return [e[:width] + ("..." if len(e) > width else "") for e in entries]

    def _success_response(self, target: str, message: str = None) -> Dict[str, Any]:
        # A successful write means the consolidation loop made progress, so
        # the per-turn failure budget resets (the cap counts consecutive
        # failures, not lifetime ones within a turn).
        self._consolidation_failures = 0
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        # Terminal: confirms the write landed, tells the model to stop. We do
        # NOT echo the full entries here — dumping them invites the model to
        # "find more to fix" and re-issue the same ops (observed thrash).
        resp: Dict[str, Any] = {
            "success": True,
            "done": True,
            "target": target,
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        resp["note"] = "Write saved. This update is complete — do not repeat it."
        return resp

    def _render_block(self, target: str, entries: List[str]) -> str:
        """Render a system-prompt block with header and usage indicator."""
        if not entries:
            return ""
        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        if target == "user":
            header = f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"
        else:
            header = f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    # ------------------------------------------------------------------
    # Write context manager — removed
    # ------------------------------------------------------------------
    # An earlier version wrapped each mutation in a `_safe_write` context
    # manager. It inverted the drift check (treating the clean path as an
    # error) and nested a `contextmanager` import inside the class body,
    # which is fragile. The mutation methods now call `_reload_target`
    # directly and check the returned backup-path, matching the upstream
    # Hermes shape. See `add` / `replace` / `remove` / `apply_batch`.


def _drift_error(path: Path, bak: Optional[str]) -> Dict[str, Any]:
    """Build the error dict returned when external drift is detected.

    The on-disk memory file contains content that wouldn't round-trip through
    the tool's parser/serializer — flushing would discard the appended/edited
    content from a patch tool, shell append, manual edit, or sister-session
    write. Refuse the mutation, point the operator at the .bak snapshot, and
    tell them what to do next.
    """
    return {
        "success": False,
        "done": True,
        "error": (
            f"Refusing to write {path.name}: on-disk content was edited externally and "
            f"doesn't match the tool's format. A backup was saved to {bak}. "
            f"To recover: inspect the backup, then either (a) edit {path.name} by hand "
            f"to match the §-delimited format, or (b) delete it and let the pet start "
            f"fresh. No write was performed."
        ),
        "backup": bak,
    }
