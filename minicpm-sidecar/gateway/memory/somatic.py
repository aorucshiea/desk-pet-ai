"""身体感受 (somatic sense) — the body reports touch to the brain.

The Electron shell (the pet's BODY) reports physical interactions:
being dragged across the screen, being clicked / poked. The gateway
keeps a short-fresh buffer of those sensations, injects a compact
【身体感受】block into the next chat's system prompt (so the pet KNOWS
it was picked up — 具身认知), and writes light episodic events so
meaningful touches survive as recall-able memory.

Design notes:
- The buffer is in-memory ONLY: fresh sensations, not a journal.
  Durable memory goes through the episodic event store (throttled per
  kind) — the same store dreams and resonance read.
- All functions are pure over (buffer, now) so tests can inject clocks.
- The body spams: drag-move fires per frame, clicks come in bursts.
  Coalescing + write-cooldown keep both the buffer and the memory
  stream calm.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone

# A sensation stops being "fresh" (and stops being injected) after this.
FRESH_WINDOW_SECONDS = 600
MAX_BUFFER = 24

# Identical sensations within this window coalesce into one entry.
COALESCE_SECONDS = 20

# Episodic event writes are throttled per kind — a gesture storm should
# not flood the memory stream. The LATEST phrasing is what gets written.
EVENT_WRITE_COOLDOWN_SECONDS = 300
EVENT_WEIGHT = 160  # light: recall-able, fades fast

# Drag distance thresholds (screen px) for phrasing.
DRAG_NEAR_PX = 400
DRAG_FAR_PX = 1500

# Click-burst phrasing thresholds.
CLICK_POKE = 2
CLICK_POKY = 4


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _positive_int(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


class SensationBuffer:
    """Thread-safe short-term buffer of recent body sensations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: deque = deque(maxlen=MAX_BUFFER)
        self._last_event_write: dict[str, datetime] = {}

    def add(self, kind: str, detail: str, at: datetime) -> None:
        with self._lock:
            # Coalesce: refresh the matching recent sensation instead of
            # appending a near-duplicate (drag-move spam, jittery clicks).
            for item in reversed(self._items):
                if item[0] == kind and (at - item[2]).total_seconds() <= COALESCE_SECONDS:
                    self._items.remove(item)
                    self._items.append((kind, detail, at))
                    return
            self._items.append((kind, detail, at))

    def fresh(self, now: datetime) -> list:
        with self._lock:
            return [
                (kind, detail, at)
                for (kind, detail, at) in self._items
                if (now - at).total_seconds() <= FRESH_WINDOW_SECONDS
            ]

    def should_write_event(self, kind: str, now: datetime) -> bool:
        with self._lock:
            last = self._last_event_write.get(kind)
            if last is not None and (now - last).total_seconds() < EVENT_WRITE_COOLDOWN_SECONDS:
                return False
            self._last_event_write[kind] = now
            return True


def drag_phrase(distance_px: int, duration_ms: int) -> str:
    if distance_px >= DRAG_FAR_PX:
        where = "搬到了很远的地方"
    elif distance_px >= DRAG_NEAR_PX:
        where = "搬到了不远处"
    else:
        where = "挪了挪位置"
    if duration_ms >= 2000:
        return f"{where}（被拎了 {duration_ms // 1000} 秒）"
    return where


def click_phrase(clicks: int) -> str:
    if clicks >= CLICK_POKY:
        return f"用户用鼠标点了你 {clicks} 下，戳个不停"
    if clicks >= CLICK_POKE:
        return f"用户用鼠标点了你 {clicks} 下"
    return "用户用鼠标点了一下你"


def record(buffer: SensationBuffer, payload: dict, *, now: datetime | None = None):
    """Record one body interaction.

    Returns (detail, event_title, event_content); the event fields are
    None when the kind is unknown or the per-kind write cooldown is
    still running.
    """
    now = now or _now()
    kind = str(payload.get("kind") or "").strip().lower()
    if kind == "drag":
        detail = f"你被用户拎起来，{drag_phrase(_positive_int(payload.get('distance_px')), _positive_int(payload.get('duration_ms')))}"
        title, content = "被用户拎起移动", detail
        buffer.add(kind, detail, now)
    elif kind == "click":
        clicks = _positive_int(payload.get("clicks")) or 1
        detail = click_phrase(clicks)
        title, content = "被用户点击", detail
        buffer.add(kind, detail, now)
    else:
        return (None, None, None)

    if not buffer.should_write_event(kind, now):
        return (detail, None, None)
    return (detail, title, content)


def _rel_time(delta_seconds: float) -> str:
    if delta_seconds < 60:
        return "刚刚"
    if delta_seconds < 300:
        return "几分钟前"
    return "不久前"


def build_somatic_block(buffer: SensationBuffer, now: datetime | None = None) -> str:
    """Render fresh sensations as a system-prompt block ("" when none)."""
    now = now or _now()
    lines = []
    for kind, detail, at in buffer.fresh(now):
        rel = _rel_time((now - at).total_seconds())
        lines.append(f"- {rel}，{detail}")
    if not lines:
        return ""
    return (
        "【身体感受】这是你的身体刚刚传来的触觉——用户在真实地碰你。"
        "可以自然地把它们揉进你说的话里，但不要机械复述：\n" + "\n".join(lines)
    )
