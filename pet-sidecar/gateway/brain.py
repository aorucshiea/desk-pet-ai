"""In-process "brain stream" bus for the brain window.

A tiny pub/sub with a recent-event ring buffer. ``_stream_chat_provider``
publishes structured round events here and ``/api/brain/stream`` forwards
them to the UI over SSE.

Failure isolation is the contract: publishing is a side channel and must
never break a chat turn. Bad payloads are coerced into a JSON-safe shape,
subscriber queues are best-effort (drop-oldest when full), and every queue
put is wrapped.
"""

from __future__ import annotations

import asyncio
import copy
import math
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List

RING_MAX = 600
TEXT_MAX = 4000
META_STRING_MAX = 1200
SUBSCRIBER_QUEUE_MAX = RING_MAX

_lock = threading.Lock()
_next_id = 0
_ring: "deque[dict]" = deque(maxlen=RING_MAX)
_subscribers: "set[asyncio.Queue]" = set()


def _clip(value: Any, limit: int) -> str:
    try:
        text = "" if value is None else str(value)
    except Exception:
        text = "<unprintable>"
    return text[:limit] if len(text) > limit else text


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _json_safe(value: Any, *, string_limit: int | None = None) -> Any:
    """Recursively coerce a value so ``json.dumps`` cannot fail on it."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value[:string_limit] if string_limit else value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item, string_limit=string_limit)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [
            _json_safe(item, string_limit=string_limit)
            for item in value
        ]
    try:
        text = str(value)
    except Exception:
        text = "<unprintable>"
    return text[:string_limit] if string_limit else text


def _timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _offer(queue: asyncio.Queue, event: dict) -> None:
    """Best-effort put: drop the oldest event when a subscriber is slow."""
    try:
        queue.put_nowait(event)
        return
    except asyncio.QueueFull:
        pass
    except Exception:
        return
    try:
        queue.get_nowait()
    except Exception:
        pass
    try:
        queue.put_nowait(event)
    except Exception:
        pass


def publish(event: dict) -> dict:
    """Assign id/ts, append to the ring and fan out. Never raises."""
    global _next_id
    source = event if isinstance(event, dict) else {}
    meta = source.get("meta")
    if not isinstance(meta, dict):
        meta = {}

    with _lock:
        _next_id += 1
        try:
            record = {
                "id": _next_id,
                "ts": _timestamp(),
                "round": _clip(source.get("round") or "", 120),
                "iteration": _as_int(source.get("iteration")),
                "type": _clip(source.get("type") or "event", 80),
                "title": _clip(source.get("title") or "", 200),
                "text": _clip(source.get("text") or "", TEXT_MAX),
                "meta": _json_safe(meta, string_limit=META_STRING_MAX),
            }
        except Exception:
            # Pathological payload (e.g. a recursive meta dict): still emit a
            # usable placeholder rather than raising into the chat turn.
            record = {
                "id": _next_id,
                "ts": _timestamp(),
                "round": "",
                "iteration": 0,
                "type": "event",
                "title": "",
                "text": "",
                "meta": {},
            }
        _ring.append(record)
        queues = list(_subscribers)

    for queue in queues:
        _offer(queue, record)
    return copy.deepcopy(record)


def subscribe() -> asyncio.Queue:
    """Register a live subscriber queue (bounded, drop-oldest on publish)."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
    with _lock:
        _subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    with _lock:
        _subscribers.discard(queue)


def snapshot() -> List[Dict[str, Any]]:
    """Copy of the current ring buffer, oldest first."""
    with _lock:
        return copy.deepcopy(list(_ring))


def clear() -> None:
    """Reset ring, sequence, subscribers and their pending events (tests)."""
    global _next_id
    with _lock:
        queues = list(_subscribers)
        _ring.clear()
        _subscribers.clear()
        _next_id = 0
    for queue in queues:
        while True:
            try:
                queue.get_nowait()
            except Exception:
                break


__all__ = [
    "RING_MAX",
    "TEXT_MAX",
    "META_STRING_MAX",
    "publish",
    "subscribe",
    "unsubscribe",
    "snapshot",
    "clear",
]
