"""LingLing weight decay engine — forgetting as a feature.

Percentage-based decay (not fixed-value). Important events naturally
resist forgetting because they have more absolute weight to lose.
Decay is faster early and slows over time — the Ebbinghaus curve.

The decay runner is meant to be called periodically (every ~10 minutes)
by a background task in server.py. It is not a standalone daemon.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from ..log_setup import get_logger
from .events import EventStore

logger = get_logger()

# How much of the CURRENT weight decays per hour (5%).
# This is a percentage, not a fixed value. Weight-900 events lose 45/hour;
# weight-100 events lose 5/hour. Important things are naturally harder to forget.
DECAY_RATE_PER_HOUR = 0.05

# How often the decay runner executes (seconds). Every 10 minutes.
DECAY_INTERVAL_SECONDS = 600

# Consolidation factor: each access multiplies decay_coefficient by this.
# After ~7 accesses: 0.7^7 ≈ 0.08 — the event barely decays at all.
# "想起十次就忘不掉。"
CONSOLIDATION_FACTOR = 0.7

# Minimum decay coefficient — events never become completely immutable.
MIN_DECAY_COEFFICIENT = 0.05

# Weight boost when an event is accessed or mentioned.
ACCESS_WEIGHT_BOOST = 2


def run_decay(store: EventStore) -> int:
    """Run one decay pass over all events.

    Called every DECAY_INTERVAL_SECONDS by a background task.
    Returns the number of events that had weight reduced.
    """
    now = datetime.now(timezone.utc)
    decayed = 0

    for evt in store.get_all_events():
        if evt.get("pause_decay"):
            continue

        last_accessed = _parse_time(evt.get("last_accessed", ""))
        hours_since = (now - last_accessed).total_seconds() / 3600.0

        if hours_since <= 0:
            continue

        # Base decay: percentage of current weight, scaled by time elapsed
        # and by how often the decay runner fires (to keep the rate per-hour).
        base_decay = evt["weight"] * DECAY_RATE_PER_HOUR * (hours_since / (DECAY_INTERVAL_SECONDS / 3600.0))

        # Multiply by decay_coefficient (consolidated events decay slower)
        actual_decay = base_decay * evt["decay_coefficient"]

        old_weight = evt["weight"]
        evt["weight"] = max(0.0, evt["weight"] - actual_decay)

        if evt["weight"] < old_weight:
            decayed += 1

    if decayed > 0:
        store.save()
        logger.debug("Decay pass: %d events decayed", decayed)

    return decayed


def on_event_accessed(store: EventStore, event_id: str) -> None:
    """Called when an event is loaded into context or recalled.

    Effects:
      - weight +2 (small boost for being remembered)
      - decay_coefficient × 0.7 (consolidation — remembers slower next time)
      - access_count + 1
      - last_accessed = now
    """
    evt = store.get_event(event_id)
    if evt is None:
        return

    evt["weight"] = min(999, evt["weight"] + ACCESS_WEIGHT_BOOST)
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
    evt["weight"] = min(999, evt["weight"] + ACCESS_WEIGHT_BOOST)
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
