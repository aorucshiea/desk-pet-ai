"""MemoryContext — theme-scoped memory (换身体 = 换灵魂).

Each animation theme (身体) owns its own soul-layer memory: episodic
events, identity notes (MEMORY.md / USER.md), mood, and resonance
vectors. Switching themes hot-swaps all of them; the capability layer
(skills / experiences / cognition) stays global — 会做的事不随皮相变.

Layout:
  {memory_dir}/                      ← "default" theme (zero migration)
  {memory_dir}/themes/{slug}/        ← every other theme

The store singletons (tool._memory_store, recall._event_store,
resonance vectors, loader session) are re-injected on switch so every
existing code path keeps working unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .memory.events import EventStore
from .memory.mood import MoodStore
from .memory.store import MemoryStore
from .memory import loader
from .memory import resonance as _resonance_module

# Theme ids are lowercase ASCII slugs (create-theme.js slugify). Mirror it
# here so an arbitrary themeId can never escape the themes/ directory.
_THEME_SLUG_RE = re.compile(r"[^a-z0-9]+")


def theme_slug(theme_id: Optional[str]) -> str:
    """Normalize a theme id into a filesystem-safe slug."""
    if not theme_id:
        return "default"
    slug = _THEME_SLUG_RE.sub("-", theme_id.lower()).strip("-")
    return slug or "default"


class MemoryContext:
    """Holds the three soul-layer stores and switches them per theme."""

    def __init__(self, base_dir: Path) -> None:
        self._base = Path(base_dir).expanduser()
        self._base.mkdir(parents=True, exist_ok=True)
        self.memory_store = MemoryStore()
        self.event_store = EventStore()
        self.mood_store = MoodStore()
        self.current_theme = "default"
        self._booted = False

    # ------------------------------------------------------------------
    # Theme dirs
    # ------------------------------------------------------------------

    def theme_dir(self, theme_id: Optional[str]) -> Path:
        """Directory for a theme's memory files. "default" (or unknown /
        empty) maps to the base dir — existing data stays put."""
        slug = theme_slug(theme_id)
        if slug == "default":
            return self._base
        return self._base / "themes" / slug

    def _inject_singletons(self) -> None:
        """Point the module-level singletons at the active stores."""
        from .memory.tool import set_memory_store
        from .memory.recall import set_event_store
        set_memory_store(self.memory_store)
        set_event_store(self.event_store)
        _resonance_module.set_vector_store(self.theme_dir(self.current_theme))
        loader.reset_session()

    # ------------------------------------------------------------------
    # Switching
    # ------------------------------------------------------------------

    def switch(self, theme_id: Optional[str]) -> str:
        """Load the given theme's memory. Returns the resolved slug."""
        slug = theme_slug(theme_id)
        if slug == self.current_theme and self._booted:
            return slug
        d = self.theme_dir(slug)
        d.mkdir(parents=True, exist_ok=True)
        self.memory_store.load_from_disk(d)
        self.event_store.load_from_disk(d)
        self.mood_store.load_from_disk(d)
        self.current_theme = slug
        self._booted = True
        self._inject_singletons()
        return slug
