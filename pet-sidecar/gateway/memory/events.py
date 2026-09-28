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
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..log_setup import get_logger

logger = get_logger()

# Fixed emotion vocabulary for per-sentence emotion flow annotations
# (情绪流编码). The 0.9B model must pick from these — free-form emotion
# words would destroy consistency.
EMOTION_VOCAB: List[str] = [
    "平静", "开心", "难过", "疑惑", "生气", "兴奋",
    "疲惫", "平淡", "紧张", "期待",
]

# Content hard cap — emotion-flow format makes content longer, and
# top-5 loads full content into context every turn.
CONTENT_MAX_CHARS = 500

# Duplicate guard: the same reply text can legitimately reach extraction
# twice by design (the gateway-side death-note hook runs first so a
# client that died mid-stream loses nothing; the renderer then POSTs the
# same text here). Exact-same content within this window is treated as
# the same extraction replayed, not a new event.
DEDUP_WINDOW_SECONDS = 600

# Core memory bank (核心记忆): nearly-immortal memories the model chose
# to keep. Cap enforced at extraction/promotion — putting something in
# means it almost never fades, so it must stay rare.
CORE_CAP = 7
# Auto-promote events at/above this weight (提取 prompt 的
# "改变关系的事 800-999" 档).
CORE_PROMOTE_WEIGHT = 800


def human_time_ago(created_at: str) -> str:
    """Turn a created_at ISO timestamp into a NEGATIVE time offset.

    Memory timestamps are always "-X分钟/-X小时/-X天/-X个月" — how long
    ago the event happened relative to now (user: 时间都是负数，表示记忆
    这个事件的时候距离现在过去了多久). We deliberately do NOT use
    "昨天/今天" so the label never collides with the model's own use of
    those words mid-conversation; the now-anchor line in the memory block
    lets the model convert to absolute dates.

    Lives here (not in recall.py) so loader.py can use it without a
    circular import.
    """
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return "-很久"

    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 60:
        return "-刚刚"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"-{minutes}分钟"
    hours = minutes // 60
    if hours < 24:
        return f"-{hours}小时"
    days = hours // 24
    if days < 30:
        return f"-{days}天"
    months = days // 30
    if months < 12:
        return f"-{months}个月"
    return "-很久"


def aggregate_emotion(content: str) -> str:
    """Extract the event-level emotion from an emotion-flow content.

    Counts ``（情绪）`` / ``(情绪)`` tags (from EMOTION_VOCAB) inside the
    content and returns the most frequent one; empty string if none.
    The model never writes a separate emotion field — the system derives
    it from the flow, so the directory tag always matches the content.
    """
    tags = re.findall(r"[（(]([^（）()]{1,4})[）)]", content or "")
    counts: Dict[str, int] = {}
    for word in tags:
        if word in EMOTION_VOCAB:
            counts[word] = counts.get(word, 0) + 1
    if not counts:
        return ""
    return max(counts, key=counts.get)


def parse_event_block(response_text: str) -> Optional[Dict[str, Any]]:
    """Extract the model's event/mood JSON from a reply.

    Preferred: the ``<<<MEM>>>{…}<<<MEMEND>>>`` delimited block the model is
    instructed to emit at the end of every turn. Fallback: a bare
    ``{…"events"…}`` object for older replies / other providers.

    Returns the parsed JSON dict, or None if nothing parseable.
    """
    if not response_text:
        return None

    block_match = re.search(
        r"<<<MEM>>>\s*(\{.*?\})\s*<<<MEMEND>>>", response_text, re.DOTALL
    )
    if block_match:
        try:
            return json.loads(block_match.group(1))
        except Exception:
            pass

    legacy_match = re.search(r'\{[^{}]*"events"[^{}]*\}', response_text, re.DOTALL)
    if legacy_match:
        try:
            return json.loads(legacy_match.group())
        except Exception:
            return None
    return None


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
        emotion: Optional[str] = None,
        type_: str = "experience",
        resolved: bool = True,
        core: bool = False,
        conversation_tokens: int = 0,
    ) -> Dict[str, Any]:
        """Add a new event. Called by the model after conversation ends.

        Args:
            title: Short title (<=15 chars), model-generated.
            content: First-person emotion-flow description ("句子（情绪）"
                format), model-generated. Capped at CONTENT_MAX_CHARS.
            weight: 1-999, model's judgment of importance.
            emotion: Event-level emotion. When None, derived automatically
                from the emotion-flow tags in content (aggregate_emotion).
            type_: "experience" (lived through) or "knowledge" (learned).
            resolved: False = unfinished business (Zeigarnik — the pet
                hasn't let it go); True = closed.
            core: True = nearly-immortal memory the model chose to keep
                (core memory bank, cap enforced at extraction).
            conversation_tokens: rough size of the conversation turn this
                event came from (serves as engagement proxy for decay).

        Returns:
            The created event dict.
        """
        title = (title or "").strip()[:15]  # hard cap
        content = (content or "").strip()[:CONTENT_MAX_CHARS]
        weight = max(1, min(999, int(weight)))
        if emotion is None or not str(emotion).strip():
            emotion = aggregate_emotion(content)
        type_ = type_ if type_ in ("experience", "knowledge") else "experience"

        now = datetime.now(timezone.utc)

        dup = self._find_duplicate(content, title, now)
        if dup is not None:
            logger.info(
                "EventStore: duplicate content within %ds window, "
                "returning existing %s instead of adding",
                DEDUP_WINDOW_SECONDS, dup["id"],
            )
            return dup

        evt: Dict[str, Any] = {
            "id": f"evt_{now.isoformat()[:10].replace('-', '')}_{self._next_id:03d}",
            "title": title,
            "content": content,
            "weight": weight,
            "created_at": now.isoformat(),
            "last_accessed": now.isoformat(),
            "access_count": 0,
            "decay_coefficient": 1.0,
            "pause_decay": False,
            "emotion": emotion,
            "type": type_,
            "resolved": bool(resolved),
            "core": bool(core),
            "conversation_tokens": max(0, int(conversation_tokens or 0)),
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

    def _find_duplicate(
        self, content: str, title: str, now: datetime
    ) -> Optional[Dict[str, Any]]:
        """Return an existing event with identical title AND content
        created within DEDUP_WINDOW_SECONDS of now, or None.

        Both fields must match: the death-note replay carries the same
        title+content pair, while distinct events that merely share a
        content string (test fixtures, template-y writes) stay distinct.
        """
        if not content:
            return None
        for evt in self._events:
            if (evt.get("content") or "") != content or (evt.get("title") or "") != title:
                continue
            try:
                created = datetime.fromisoformat(
                    str(evt.get("created_at", "")).replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if abs((now - created).total_seconds()) <= DEDUP_WINDOW_SECONDS:
                return evt
        return None

    @staticmethod
    def _normalize_event(evt: Dict[str, Any]) -> None:
        """Ensure an event dict has all required fields with correct types."""
        evt.setdefault("id", f"evt_unknown_{id(evt)}")
        evt.setdefault("title", "")
        evt.setdefault("content", "")
        evt["weight"] = max(0, min(999, float(evt.get("weight", 100))))
        evt.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        evt.setdefault("last_accessed", datetime.now(timezone.utc).isoformat())
        # last_decay_at defaults to created_at so a legacy event's first
        # decay pass only bills the time since it was created.
        evt.setdefault("last_decay_at", evt["created_at"])
        evt["access_count"] = int(evt.get("access_count", 0))
        evt["decay_coefficient"] = max(0.05, float(evt.get("decay_coefficient", 1.0)))
        evt["pause_decay"] = bool(evt.get("pause_decay", False))
        # P1 multi-axis fields (defaults for backward compat with v1 data).
        evt["emotion"] = str(evt.get("emotion", "") or aggregate_emotion(evt.get("content", "")))
        evt["type"] = evt.get("type", "experience") if evt.get("type") in ("experience", "knowledge") else "experience"
        evt["resolved"] = bool(evt.get("resolved", True))
        evt["core"] = bool(evt.get("core", False))
        evt["conversation_tokens"] = max(0, int(evt.get("conversation_tokens", 0)))


def promote_core(
    store: "EventStore",
    core_names: Optional[List[str]] = None,
    *,
    auto_weight: int = CORE_PROMOTE_WEIGHT,
    cap: int = CORE_CAP,
) -> int:
    """Promote events into the nearly-immortal core bank (核心记忆库).

    The model's explicit picks (by title, from the extraction block's
    top-level ``core`` array) run first; then any event at/above
    ``auto_weight`` fills the remaining room ("改变关系的事" 档).
    Core memories decay ~not at all and get a recall probability bonus.

    Returns the number of events promoted. Shared by the extraction
    endpoint and the dream consolidation cycle.
    """
    names = [n for n in (core_names or []) if isinstance(n, str) and n]
    existing_core = sum(1 for e in store.get_all_events() if e.get("core"))
    if existing_core >= cap:
        return 0
    budget = cap - existing_core
    promoted = 0

    # Model's manual picks take priority.
    if names:
        for evt in store.get_all_events():
            if promoted >= budget:
                break
            if evt.get("core"):
                continue
            if evt.get("title") in names:
                evt["core"] = True
                promoted += 1

    # Auto-promote very high-weight events while there's room.
    if promoted < budget:
        for evt in sorted(
            store.get_all_events(),
            key=lambda e: e.get("weight", 0),
            reverse=True,
        ):
            if promoted >= budget:
                break
            if evt.get("core"):
                continue
            if evt.get("weight", 0) >= auto_weight:
                evt["core"] = True
                promoted += 1

    if promoted:
        store.save()
    return promoted
