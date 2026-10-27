"""Pure scheduling and persistent state for automatic sleep compression.

Provider calls and persistence of extracted memories live in ``sleep_compress``
and the sleep organ.  This module only decides which old, sufficiently active
days are eligible and records days that completed successfully.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import conversation_log

STATE_FILE = "sleep-state.json"
DEFAULT_IDLE_SECONDS = 7200
DEFAULT_MIN_TURNS = 4


def load_state(memory_dir: Path) -> Dict[str, Any]:
    path = Path(memory_dir) / STATE_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_state(memory_dir: Path, state: Dict[str, Any]) -> None:
    path = Path(memory_dir) / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".sleep_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _today(value: Optional[Any] = None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def candidate_days(
    memory_dir: Path,
    *,
    state: Optional[Dict[str, Any]] = None,
    min_turns: int = DEFAULT_MIN_TURNS,
    today: Optional[Any] = None,
) -> List[str]:
    """Old, uncompleted log days with at least ``min_turns`` JSONL records."""
    root = Path(memory_dir)
    current = _today(today)
    completed = set((state if state is not None else load_state(root)).get("compressed_days") or [])
    threshold = max(1, int(min_turns))
    candidates = []
    for day in conversation_log.available_days(root):
        if day in completed or not conversation_log.valid_day(day):
            continue
        try:
            if date.fromisoformat(day) >= current:
                continue
        except ValueError:
            continue
        if len(conversation_log.read_day(day, root)) >= threshold:
            candidates.append(day)
    return candidates


def should_run(
    memory_dir: Path,
    last_conversation_at: Optional[datetime],
    *,
    state: Optional[Dict[str, Any]] = None,
    idle_seconds: int = DEFAULT_IDLE_SECONDS,
    min_turns: int = DEFAULT_MIN_TURNS,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """Gate one sleep cycle without doing model or EventStore work."""
    if last_conversation_at is None:
        return False, "never_talked"
    current = _as_utc(now or datetime.now(timezone.utc))
    try:
        idle = (current - _as_utc(last_conversation_at)).total_seconds()
    except (TypeError, ValueError):
        return False, "invalid_last_conversation"
    if idle < max(0, int(idle_seconds)):
        return False, "not_idle"
    candidates = candidate_days(
        memory_dir, state=state, min_turns=min_turns, today=current.date(),
    )
    if not candidates:
        return False, "no_candidates"
    return True, candidates[0]


def mark_completed(
    memory_dir: Path,
    day: str,
    result: Optional[Dict[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Record success only; callers must not invoke this for failed cycles."""
    root = Path(memory_dir)
    state = load_state(root)
    completed = list(dict.fromkeys(state.get("compressed_days") or []))
    if day not in completed:
        completed.append(day)
    current = _as_utc(now or datetime.now(timezone.utc))
    state.update({
        "compressed_days": sorted(completed),
        "last_run_at": current.isoformat(),
        "last_result": {"status": "ok", "day": day, **(result or {})},
    })
    save_state(root, state)
    return state
