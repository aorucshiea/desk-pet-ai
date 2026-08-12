"""LingLing two-layer memory loader + faded directory (v2).

Builds the episodic memory context block injected into the system prompt
at conversation start. Four felt layers:

1. **Top-N (deterministic)** — full title + content, always loaded.
   Core memories (核心记忆库) always take seats here, unconditionally.
2. **Flashback (probabilistic)** — one random event weighted by
   weight / (1000 + event_count × 10). The denominator grows with the
   memory library — more memories, each individual one less likely to
   surface. "突然想起" — the model didn't choose to remember, it just
   surfaced.
3. **Faded directory** — titles of remaining events, truncated by
   weight ratio, tagged with felt labels: [情绪] / [知识] / （还没放下）.
   Low-weight events show only a few characters. Below 3 chars: gone
   entirely. Not "I can't remember" — it's genuinely forgotten.
4. The model doesn't see weight numbers. It sees its own memory fading.

v2 (2026-08-01): felt labels carry the four-dimension virtual-feeling
signal (familiarity via fade ratio, emotion via tag, unfinishedness via
（还没放下）, knowledge-vs-experience via [知识]).
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

# Base probability denominator for the flashback layer; grows with the
# number of stored events (memory-library dilution).
FLASHBACK_PROBABILITY_DIVISOR = 1000
FLASHBACK_DILUTION_PER_EVENT = 10

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
    """Deterministic top-N loading: core memories unconditionally, then
    highest-weight events to fill the remaining seats.

    Core memories always take a seat (they're what the pet keeps on its
    mind), so with k core memories the top layer holds max(n, k) events.

    Every event loaded this way gets an access boost (consolidation).
    """
    events = sorted(
        [e for e in store.get_all_events() if e["weight"] > 0],
        key=lambda e: e["weight"],
        reverse=True,
    )
    core = [e for e in events if e.get("core")]
    rest = [e for e in events if not e.get("core")]
    top = core + rest[: max(0, n - len(core))]
    for evt in top:
        on_event_accessed(store, evt["id"])
        add_to_session(evt["id"])
    return top


def pick_flashback(
    store: EventStore,
    exclude_ids: Set[str],
) -> Optional[Dict[str, Any]]:
    """Probabilistic flashback: one random event from the remainder.

    Each candidate has probability weight / denominator of being picked,
    where denominator = 1000 + event_count × 10. Higher-weight events are
    more likely to surface spontaneously, and a bigger memory library
    dilutes any single event's chance (人脑也是记忆多了，单条更难浮现).
    """
    denominator = FLASHBACK_PROBABILITY_DIVISOR + store.event_count() * FLASHBACK_DILUTION_PER_EVENT
    candidates = [
        e for e in store.get_all_events()
        if e["id"] not in exclude_ids
        and e["id"] not in _session_loaded_ids
        and e["weight"] > 0
    ]
    random.shuffle(candidates)

    for evt in candidates:
        if random.random() < (evt["weight"] / denominator):
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
    Entries carry felt labels (v2): [情绪] tag, [知识] for knowledge
    memories (kept dim — knowledge is not lived experience), and
    （还没放下） for unfinished business (Zeigarnik — the pet hasn't
    let it go).
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
        prefix = ""
        if evt.get("emotion"):
            prefix += f"[{evt['emotion']}] "
        if evt.get("type") == "knowledge":
            prefix += "[知识] "
        suffix = ""
        if not evt.get("resolved", True) and evt["weight"] >= 200:
            suffix = "（还没放下）"
        lines.append(f"- {prefix}{faded}{suffix}")

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

    core_events = [e for e in top_events if e.get("core")]
    normal_events = [e for e in top_events if not e.get("core")]

    # Layer 0: Core memories — what the pet keeps on its mind (P5).
    # Nearly immortal, always loaded, presented as its own layer so the
    # model reads them as identity anchors, not ordinary memories.
    if core_events:
        lines.append("【你一直放在心上的事】")
        for i, evt in enumerate(core_events, 1):
            lines.append(f"{i}. [{evt['title']}] {evt['content']}")

    # Layer 1: Full memories
    if normal_events:
        if lines:
            lines.append("")
        lines.append("【你记得的事】")
        for i, evt in enumerate(normal_events, 1):
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
