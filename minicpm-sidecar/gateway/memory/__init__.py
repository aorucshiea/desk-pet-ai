"""Persistent cross-session memory for the desk pet.

Two layers:

  1. **Identity memory** (store.py): MEMORY.md + USER.md — who the user is,
     who the pet is. Bounded, curated by the model via the ``memory`` tool.

  2. **Episodic memory** (events.py + decay.py + loader.py): Weighted events
     with time-based decay. Things that happened, weighted by importance,
     fading over time unless recalled. The LingLing innovation.

Design notes for the identity layer (from upstream hermes-agent):

  - Two files: ``MEMORY.md`` (the pet's own notes) + ``USER.md`` (what the
    pet knows about the user). Both live under ``<userData>/memories/``.
  - **Frozen snapshot pattern**: ``load_from_disk()`` captures a snapshot
    that ``format_for_system_prompt()`` returns for system-prompt injection.
    Mid-session tool writes persist to disk immediately but do NOT mutate
    the snapshot — the system prompt stays stable for the whole session,
    preserving any provider prefix cache. The snapshot refreshes next boot.
  - Bounded by **character** limits (not tokens) so the cap is
    model-independent: memory 2200, user 1375.
  - Entries are delimited by ``\n§\n`` (section sign) and may be multiline.
  - ``add`` / ``replace`` / ``remove`` use short unique substring matching.
  - Atomic write via temp-file + ``os.replace()`` — readers always see a
    complete file, never a half-written one.

Design notes for the episodic layer (LingLing):

  - Events are stored in ``events.json`` — structured, weighted, decaying.
  - Weights are model-assigned (1-999). The model decides what matters.
  - Decay is percentage-based (5%/hour of current weight). Important
    events naturally resist forgetting.
  - Consolidation: each access reduces decay_coefficient × 0.7.
    Remember something enough times and it becomes nearly permanent.
  - Two-layer loading: top-5 deterministic + 1 flashback probabilistic.
  - Faded directory: non-loaded events show truncated titles.
    Below 3 visible chars: gone entirely.
  - Embedding resonance: user messages semantically match stored events.
  - Mood autonomy: the model self-assesses its mood after each turn.
    The mood persists across restarts and shapes the next conversation.
"""

from __future__ import annotations

from .store import (
    ENTRY_DELIMITER,
    MemoryStore,
)
from .events import EventStore
from .mood import MoodStore

__all__ = [
    "ENTRY_DELIMITER",
    "MemoryStore",
    "EventStore",
    "MoodStore",
]
