"""Per-skill activity tracking — the curator's data source.

Keeps a small per-skill record (use_count, last_activity_at, state, pinned,
created_at) in a single JSON file under the memory dir. The SKILL.md files
themselves stay pure and auditable — usage metadata lives here, separate,
so editing a skill's Markdown never touches its activity clock and vice
versa. Mirrors hermes-agent's ``tools/skill_usage`` split.

State machine (curator drives the transitions):

  active  ──(stale_after_days no use)──►  stale
  stale   ──(archive_after_days no use)──►  archived
  stale   ──(used again)──────────────────►  active   (reactivate)
  pinned  ── never auto-transitioned ──────────────────

Archive is RECOVERABLE: the SKILL.md moves to an ``archive/`` subdir, not
deleted. ``restore_skill`` moves it back and flips state to active.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from ..log_setup import get_logger

logger = get_logger()


# Lifecycle states. Pinned is an override flag, not a state — a pinned skill
# stays in whatever state it's in and the curator skips it entirely.
STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"

_VALID_STATES = {STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED}


def _usage_file() -> Path:
    """The per-skill usage record file.

    Lives under MINICPM_MEMORY_DIR (same place as MEMORY.md/USER.md) so all
    pet state is co-located and survives restarts. Falls back to
    ``~/.minicpm/memories/`` for direct CLI runs.
    """
    env = os.environ.get("MINICPM_MEMORY_DIR", "").strip()
    if env:
        base = Path(env).expanduser()
    else:
        base = Path.home() / ".minicpm" / "memories"
    base.mkdir(parents=True, exist_ok=True)
    return base / "skill-usage.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


class SkillUsage:
    """In-memory mirror of the usage JSON, persisted on every mutation.

    One process-wide instance, owned by SkillService. Thread-safe via a
    single lock around load/read-modify-write/persist.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: Dict[str, Dict[str, Any]] = {}
        self._loaded = False
        self._archive_root: Optional[Path] = None

    def _archive_dir(self) -> Path:
        if self._archive_root is None:
            self._archive_root = _usage_file().parent / "skill-archive"
        self._archive_root.mkdir(parents=True, exist_ok=True)
        return self._archive_root

    def load(self) -> None:
        """Read the usage file. Missing/corrupt → start empty (never crash)."""
        with self._lock:
            if self._loaded:
                return
            try:
                raw = _usage_file().read_text(encoding="utf-8")
                data = json.loads(raw) if raw.strip() else {}
                self._records = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._records = {}
            self._loaded = True

    def _persist_locked(self) -> None:
        try:
            _usage_file().write_text(
                json.dumps(self._records, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("skill-usage persist failed: %s", exc)

    def _ensure_record(self, name: str) -> Dict[str, Any]:
        rec = self._records.get(name)
        if rec is None:
            rec = {
                "use_count": 0,
                "last_activity_at": None,
                "created_at": _now_iso(),
                "state": STATE_ACTIVE,
                "pinned": False,
            }
            self._records[name] = rec
        return rec

    def seed_if_missing(self, name: str) -> bool:
        """Anchor a skill's clock to now on first sight (curator calls this).

        Returns True if a new record was seeded. Without this, a built-in or
        newly-discovered skill with no usage history would archive itself on
        the first curator pass — we want its clock to start NOW.
        """
        with self._lock:
            if name in self._records:
                return False
            self._ensure_record(name)
            self._persist_locked()
            return True

    def bump(self, name: str) -> None:
        """Record a use of ``name`` (called when the skill is loaded/invoked).

        Bumps use_count, refreshes last_activity_at, and reactivates if it
        was stale (used again → back to active).
        """
        with self._lock:
            rec = self._ensure_record(name)
            rec["use_count"] = int(rec.get("use_count", 0) or 0) + 1
            rec["last_activity_at"] = _now_iso()
            if rec.get("state") == STATE_STALE:
                rec["state"] = STATE_ACTIVE  # reactivate on reuse
            self._persist_locked()

    def set_state(self, name: str, state: str) -> None:
        if state not in _VALID_STATES:
            raise ValueError(f"invalid skill state: {state}")
        with self._lock:
            rec = self._ensure_record(name)
            rec["state"] = state
            self._persist_locked()

    def set_pinned(self, name: str, pinned: bool) -> None:
        with self._lock:
            rec = self._ensure_record(name)
            rec["pinned"] = bool(pinned)
            self._persist_locked()

    def is_pinned(self, name: str) -> bool:
        self.load()
        rec = self._records.get(name)
        return bool(rec and rec.get("pinned"))

    def get(self, name: str) -> Dict[str, Any]:
        self.load()
        rec = self._records.get(name)
        if rec is None:
            # Live-read without persisting — caller (curator) will seed if needed.
            return {
                "use_count": 0,
                "last_activity_at": None,
                "created_at": _now_iso(),
                "state": STATE_ACTIVE,
                "pinned": False,
                "_persisted": False,
            }
        rec = dict(rec)
        rec["_persisted"] = True
        return rec

    def all_records(self) -> Dict[str, Dict[str, Any]]:
        """Return a snapshot of every tracked skill's record (curator uses this)."""
        self.load()
        return {name: dict(rec) for name, rec in self._records.items()}

    def archive_skill(self, name: str, source_path: Path) -> tuple[bool, str]:
        """Move the SKILL.md to archive/ and flip state to archived (recoverable).

        Returns (ok, message). Never deletes — archive is recoverable via
        restore_skill().
        """
        with self._lock:
            rec = self._ensure_record(name)
            if rec.get("state") == STATE_ARCHIVED:
                return True, "already archived"
            src = Path(source_path)
            if not src.exists():
                # File already gone (user deleted) — just flip state.
                rec["state"] = STATE_ARCHIVED
                self._persist_locked()
                return True, "source missing; state flipped to archived"
            dst = self._archive_dir() / src.name
            # Don't clobber an existing archive of a different skill.
            if dst.exists():
                dst = self._archive_dir() / f"{src.stem}-{int(datetime.now().timestamp())}{src.suffix}"
            try:
                src.rename(dst)
            except OSError as exc:
                return False, f"move failed: {exc}"
            rec["state"] = STATE_ARCHIVED
            rec["_archived_path"] = str(dst)
            self._persist_locked()
            return True, f"archived to {dst}"

    def restore_skill(self, name: str, archived_path: Path, dest_dir: Path) -> tuple[bool, str]:
        """Move an archived SKILL.md back into ``dest_dir`` and reactivate."""
        with self._lock:
            rec = self._ensure_record(name)
            src = Path(archived_path)
            if not src.exists():
                return False, f"archive file missing: {src}"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dst = dest_dir / src.name
            try:
                src.rename(dst)
            except OSError as exc:
                return False, f"restore move failed: {exc}"
            rec["state"] = STATE_ACTIVE
            rec["last_activity_at"] = _now_iso()
            rec.pop("_archived_path", None)
            self._persist_locked()
            return True, f"restored to {dst}"
