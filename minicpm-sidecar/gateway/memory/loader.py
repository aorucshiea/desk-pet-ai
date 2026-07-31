"""LingLing two-layer memory loader + faded directory.

Builds the episodic memory context block injected into the system prompt
at conversation start. Three visual layers:

1. **Top-5 (deterministic)** — full title + content, always loaded.
2. **Flashback (probabilistic)** — one random event weighted by
   weight/1000 probability. "突然想起" — the model didn't choose to
   remember, it just surfaced.
3. **Faded directory** — titles of remaining events, truncated by
   weight ratio. Low-weight events show only a few characters.
   Below 3 chars: gone entirely. Not "I can't remember" — it's
   genuinely forgotten.

The model doesn't see weight numbers. It sees its own memory fading.
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Set

from ..log_setup import get_logger
from .decay import on_event_accessed
from .events import EventStore

logger = get_logger()

# How many events go into the deterministic top layer.
TOP_N = 5

# Probability threshold for the flashback layer: weight / 1000.
FLASHBACK_PROBABILITY_DIVISOR = 1000

# Minimum visible characters for a directory title to appear at all.
# Below this, the event doesn't just look fuzzy — it's gone.
MIN_VISIBLE_CHARS = 3

# Session-level tracking of loaded event IDs. Once loaded, they stay
# in context for the entire conversation — no "一阵一阵" flickering.
_session_loaded_ids: Set[str] = set()


def reset_session() -> None:
    """Clear the session loaded set. Call at conversation start."""
    global _session_loaded_ids
    _session_loaded_ids = set()


def add_to_session(event_id: str) -> None:
    """Mark an event as loaded in this session."""
    _session_loaded_ids.add(event_id)


def is_in_session(event_id: str) -> bool:
    return event_id in _session_loaded_ids


def get_session_ids() -> Set[str]:
    return set(_session_loaded_ids)


def load_top_events(store: EventStore, n: int = TOP_N) -> List[Dict[str, Any]]:
    """Deterministic top-N loading: highest weight events.

    Every event loaded this way gets an access boost (consolidation).
    """
    events = sorted(
        [e for e in store.get_all_events() if e["weight"] > 0],
        key=lambda e: e["weight"],
        reverse=True,
    )
    top = events[:n]
    for evt in top:
        on_event_accessed(store, evt["id"])
        add_to_session(evt["id"])
    return top


def pick_flashback(
    store: EventStore,
    exclude_ids: Set[str],
) -> Optional[Dict[str, Any]]:
    """Probabilistic flashback: one random event from the remainder.

    Each candidate has probability weight/1000 of being picked.
    Higher-weight events are more likely to surface spontaneously.
    """
    candidates = [
        e for e in store.get_all_events()
        if e["id"] not in exclude_ids
        and e["id"] not in _session_loaded_ids
        and e["weight"] > 0
    ]
    random.shuffle(candidates)

    for evt in candidates:
        if random.random() < (evt["weight"] / FLASHBACK_PROBABILITY_DIVISOR):
            on_event_accessed(store, evt["id"])
            add_to_session(evt["id"])
            return evt
    return None


def build_faded_directory(
    store: EventStore,
    exclude_ids: Set[str],
) -> List[str]:
    """Build the faded-title directory for non-loaded events.

    Title visible chars = original length × (weight / 1000).
    Titles below MIN_VISIBLE_CHARS are omitted entirely.
    """
    events = sorted(
        [e for e in store.get_all_events() if e["weight"] > 0],
        key=lambda e: e["weight"],
        reverse=True,
    )
    lines: List[str] = []

    for evt in events:
        if evt["id"] in exclude_ids:
            continue
        if evt["id"] in _session_loaded_ids:
            continue

        ratio = evt["weight"] / 1000.0
        title = evt.get("title", "")
        visible = int(len(title) * ratio)

        if visible < MIN_VISIBLE_CHARS:
            continue  # Truly forgotten — not even a ghost in the directory.

        faded = title[:visible]
        lines.append(f"- {faded}")

    return lines


def build_memory_context(store: EventStore) -> str:
    """Build the complete episodic memory context for the system prompt.

    This is the primary entry point. Call once per conversation turn
    (typically from fetchSkillsContext in the renderer, via a new
    /api/events/context endpoint).
    """
    reset_session()

    top_events = load_top_events(store)
    top_ids = {e["id"] for e in top_events}

    flashback = pick_flashback(store, top_ids)
    if flashback:
        top_ids.add(flashback["id"])

    directory = build_faded_directory(store, top_ids)

    lines: List[str] = []

    # Layer 1: Full memories
    if top_events:
        lines.append("【你记得的事】")
        for i, evt in enumerate(top_events, 1):
            lines.append(f"{i}. [{evt['title']}] {evt['content']}")

    # Layer 2: Flashback
    if flashback:
        lines.append("")
        lines.append("【突然浮现在脑海中的事】")
        lines.append(f"[{flashback['title']}] {flashback['content']}")

    # Layer 3: Faded directory
    if directory:
        lines.append("")
        lines.append("【你经历过但记不清细节的事】")
        lines.extend(directory)

    if lines:
        lines.append("")
        lines.append(
            "以上是你记得自己经历过的事。有些你还记得很清楚，有些只剩下一个模糊的影子。"
            "如果你想不起某件事的细节，试着回忆。"
        )
    else:
        lines.append("【你的记忆】")
        lines.append("还没有记住任何事。这是刚开始。")

    return "\n".join(lines)
