"""LingLing event-based weighted memory — the episodic layer.

Replaces the flat MEMORY.md/USER.md text-block model with a structured
event store. Each event is a thing that *happened*, weighted by the
model's own judgment of importance, and subject to time-based decay.

The existing ``MemoryStore`` (MEMORY.md / USER.md) continues to serve as
the *identity* layer — who the user is, who the pet is. This module is
the *episodic* layer — what the pet has lived through.

Storage: ``{memory_dir}/events.json`` — single JSON file, atomic writes.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..log_setup import get_logger

logger = get_logger()


class EventStore:
    """Weighted episodic memory with file persistence.

    Every conversation turn the model can extract 0-3 events and write
    them here. On subsequent turns, the top-weighted events are injected
    into the system prompt so the pet "remembers" what happened.

    The store is deliberately simple: a single JSON file, no external
    database. This keeps the project file-based (no SQLite, no ORM).
    """

    def __init__(self) -> None:
        self._events: List[Dict[str, Any]] = []
        self._memory_dir: Optional[Path] = None
        self._next_id: int = 1

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _path(self) -> Path:
        if self._memory_dir is None:
            raise RuntimeError("EventStore used before load_from_disk")
        return self._memory_dir / "events.json"

    def load_from_disk(self, memory_dir: Path) -> None:
        """Load events from events.json, create dir if needed."""
        self._memory_dir = Path(memory_dir).expanduser()
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        path = self._path()
        if not path.exists():
            self._events = []
            self._next_id = 1
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._events = data.get("events", [])
            self._next_id = data.get("next_id", len(self._events) + 1)
            # Ensure all events have required fields
            for evt in self._events:
                self._normalize_event(evt)
        except Exception as exc:
            logger.warning("EventStore load failed, starting fresh: %s", exc)
            self._events = []
            self._next_id = 1

    def save(self) -> None:
        """Persist events to disk using atomic temp-file + rename."""
        if self._memory_dir is None:
            raise RuntimeError("EventStore.save called before load_from_disk")
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "next_id": self._next_id,
            "events": self._events,
        }
        content = json.dumps(data, ensure_ascii=False, indent=2)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), suffix=".tmp", prefix=".evt_"
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
    # CRUD
    # ------------------------------------------------------------------

    def add_event(
        self,
        title: str,
        content: str,
        weight: int,
    ) -> Dict[str, Any]:
        """Add a new event. Called by the model after conversation ends.

        Args:
            title: Short title (<=15 chars), model-generated.
            content: First-person description with feelings, model-generated.
            weight: 1-999, model's judgment of importance.

        Returns:
            The created event dict.
        """
        title = (title or "").strip()[:15]  # hard cap
        content = (content or "").strip()
        weight = max(1, min(999, int(weight)))

        now = datetime.now(timezone.utc).isoformat()

        evt: Dict[str, Any] = {
            "id": f"evt_{now[:10].replace('-', '')}_{self._next_id:03d}",
            "title": title,
            "content": content,
            "weight": weight,
            "created_at": now,
            "last_accessed": now,
            "access_count": 0,
            "decay_coefficient": 1.0,
            "pause_decay": False,
        }
        self._next_id += 1
        self._events.append(evt)
        self.save()
        logger.info(
            "EventStore: added event %s weight=%d title=%r",
            evt["id"], weight, title,
        )
        return evt

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        """Get a single event by ID."""
        for evt in self._events:
            if evt["id"] == event_id:
                return evt
        return None

    def get_all_events(self) -> List[Dict[str, Any]]:
        """Return all events (mutable references — caller can modify)."""
        return self._events

    def remove_event(self, event_id: str) -> bool:
        """Remove an event by ID. Returns True if found and removed."""
        before = len(self._events)
        self._events = [e for e in self._events if e["id"] != event_id]
        if len(self._events) < before:
            self.save()
            return True
        return False

    def search_by_keyword(self, keyword: str, limit: int = 3) -> List[Dict[str, Any]]:
        """Simple keyword search over title + content.

        Returns matching events sorted by weight (highest first).
        This is the fallback when embeddings aren't available.
        """
        if not keyword:
            return []
        keyword_lower = keyword.lower()
        matches = []
        for evt in self._events:
            if evt["weight"] <= 0:
                continue
            title = (evt.get("title") or "").lower()
            content = (evt.get("content") or "").lower()
            if keyword_lower in title or keyword_lower in content:
                matches.append(evt)
        matches.sort(key=lambda e: e["weight"], reverse=True)
        return matches[:limit]

    def event_count(self) -> int:
        return len(self._events)

    def total_weight(self) -> float:
        return sum(e.get("weight", 0) for e in self._events)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_event(evt: Dict[str, Any]) -> None:
        """Ensure an event dict has all required fields with correct types."""
        evt.setdefault("id", f"evt_unknown_{id(evt)}")
        evt.setdefault("title", "")
        evt.setdefault("content", "")
        evt["weight"] = max(0, min(999, float(evt.get("weight", 100))))
        evt.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        evt.setdefault("last_accessed", datetime.now(timezone.utc).isoformat())
        evt["access_count"] = int(evt.get("access_count", 0))
        evt["decay_coefficient"] = max(0.05, float(evt.get("decay_coefficient", 1.0)))
        evt["pause_decay"] = bool(evt.get("pause_decay", False))
