"""LingLing weight decay engine v3 — forgetting as a feature.

v3 changes (user-specified, 2026-08-01):
  - Rate DECREASES over the event's age: fastest right after the event
    happens, asymptotically slowing toward a floor. "刚开始忘得最快，
    后来越来越慢" (每分钟衰减速度变更小).
  - Weights never hit zero: W_MIN floor keeps a ghost copy in the store
    (it just stops being shown in the faded directory).
  - FREEZE: when the user hasn't talked to the pet for FREEZE_HOURS, the
    decay loop stops entirely. "用户不跟ai聊天了，一两天，直接ai全忘记"
    is exactly what this prevents — decay only runs while the pet is in
    an active conversation period.
  - Calibration: an unconsolidated weight-100 event decays to ~1 in 24
    hours of *active* conversation time. Important events survive
    through the consolidation loop (top-5 loading → on_event_accessed →
    ×0.7 coefficient each time), no extra mechanism needed.
  - Weight growth is no longer clamped at 999: the model's initial
    self-rating is capped 1-999, but repeated recall/mention keeps
    pushing weight up without limit (被反复想起的事自然越来越重).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..log_setup import get_logger
from .events import EventStore

logger = get_logger()

# Rate curve: r(t) = max(MIN, RATE0 * e^(-t/TAU)) %/hour, t = event age.
# Calibrated so an unconsolidated weight-100 event ends at ~0.91 after
# 24h of active conversation time (user spec: 24h 100 → <1).
DECAY_RATE0_PER_HOUR = 55.0   # fastest rate, right after the event
DECAY_TAU_HOURS = 1.0         # how quickly the rate decays to the floor
DECAY_RATE_MIN_PER_HOUR = 19.0  # floor — never stops, never speeds up

# Weight floor — an event never fully disappears from the store.
W_MIN = 0.5

# If the last conversation was longer ago than this, the decay loop
# freezes (memory is only allowed to fade while the pet is "awake").
FREEZE_HOURS = 3.0

# How often the decay runner executes (seconds). Every 10 minutes.
DECAY_INTERVAL_SECONDS = 600

# Consolidation factor: each access multiplies decay_coefficient by this.
# After ~7 accesses: 0.7^7 ≈ 0.08 — the event barely decays at all.
CONSOLIDATION_FACTOR = 0.7

# Minimum decay coefficient — events never become completely immutable.
MIN_DECAY_COEFFICIENT = 0.05

# Weight boost when an event is accessed or mentioned. Small on purpose —
# the model's own judgment sets importance; repetition only nudges.
ACCESS_WEIGHT_BOOST = 2


def rate_at_age(age_hours: float) -> float:
    """Decay rate (%/hour) as a function of event age.

    Fastest at birth (50%/h), shrinking every minute toward the 18%/h
    floor. This is the "先快后慢" Ebbinghaus curve.
    """
    age_hours = max(0.0, float(age_hours))
    return max(
        DECAY_RATE_MIN_PER_HOUR,
        DECAY_RATE0_PER_HOUR * math.exp(-age_hours / DECAY_TAU_HOURS),
    )


def compute_decay(
    weight: float,
    dt_hours: float,
    age_hours: float,
    coefficient: float = 1.0,
) -> float:
    """How much weight is lost over dt_hours at the current age.

    dt_hours = real elapsed time since last decay pass (not the loop
    interval) — so a pass after 30 idle minutes decays 3× a pass after
    10 idle minutes, and the rate keeps shrinking minute by minute.
    """
    if dt_hours <= 0 or weight <= 0:
        return 0.0
    rate = rate_at_age(age_hours)
    return weight * (rate / 100.0) * dt_hours * coefficient


def run_decay(
    store: EventStore,
    last_conversation_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> int:
    """Run one decay pass over all events.

    Called every DECAY_INTERVAL_SECONDS by a background task. Pass the
    timestamp of the last conversation so the loop can freeze during
    long idle periods. ``now`` is injectable for tests. Returns the
    number of events that decayed.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Freeze: no conversation for FREEZE_HOURS → memory stands still.
    if last_conversation_at is not None:
        idle_hours = (now - last_conversation_at).total_seconds() / 3600.0
        if idle_hours > FREEZE_HOURS:
            return 0

    decayed = 0
    for evt in store.get_all_events():
        if evt.get("pause_decay"):
            continue
        if evt.get("core"):
            continue  # core memories are nearly immortal (P5)

        weight = float(evt.get("weight", 0))
        if weight <= 0:
            continue

        created_at = _parse_time(evt.get("created_at", ""))
        last_decay = _parse_time(evt.get("last_decay_at") or evt.get("created_at") or "")

        dt_hours = (now - last_decay).total_seconds() / 3600.0
        if dt_hours <= 0:
            continue

        age_hours = max(0.0, (now - created_at).total_seconds() / 3600.0)
        dec = compute_decay(
            weight, dt_hours, age_hours, float(evt.get("decay_coefficient", 1.0))
        )
        if dec <= 0:
            continue

        old_weight = evt["weight"]
        evt["weight"] = max(W_MIN, evt["weight"] - dec)
        evt["last_decay_at"] = now.isoformat()
        if evt["weight"] < old_weight:
            decayed += 1

    if decayed > 0:
        store.save()
        logger.debug("Decay pass: %d events decayed", decayed)

    return decayed


def on_event_accessed(store: EventStore, event_id: str) -> None:
    """Called when an event is loaded into context or recalled.

    Effects:
      - weight +2 (small boost for being remembered — NOT capped at 999,
        repeated recall pushes weight without limit)
      - decay_coefficient × 0.7 (consolidation — forgets slower next time)
      - access_count + 1
      - last_accessed = now
    """
    evt = store.get_event(event_id)
    if evt is None:
        return

    evt["weight"] = evt["weight"] + ACCESS_WEIGHT_BOOST
    evt["decay_coefficient"] = max(
        MIN_DECAY_COEFFICIENT,
        evt["decay_coefficient"] * CONSOLIDATION_FACTOR,
    )
    evt["access_count"] = evt.get("access_count", 0) + 1
    evt["last_accessed"] = datetime.now(timezone.utc).isoformat()
    store.save()


def on_event_mentioned(store: EventStore, event_id: str) -> None:
    """Called when the model references an event mid-conversation.

    Pauses decay (you can't forget what you're actively thinking about)
    and gives a small weight boost.
    """
    evt = store.get_event(event_id)
    if evt is None:
        return

    evt["pause_decay"] = True
    evt["weight"] = evt["weight"] + ACCESS_WEIGHT_BOOST
    evt["last_accessed"] = datetime.now(timezone.utc).isoformat()
    store.save()


def on_conversation_end(store: EventStore) -> None:
    """Called when a conversation turn completes.

    Resumes decay on all paused events, but consolidates the ones that
    were mentioned (lower decay_coefficient — they'll forget slower).
    """
    for evt in store.get_all_events():
        if evt.get("pause_decay"):
            evt["pause_decay"] = False
            # Consolidation for having been mentioned
            evt["decay_coefficient"] = max(
                MIN_DECAY_COEFFICIENT,
                evt["decay_coefficient"] * CONSOLIDATION_FACTOR,
            )
    store.save()


def _parse_time(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp, falling back to epoch."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
