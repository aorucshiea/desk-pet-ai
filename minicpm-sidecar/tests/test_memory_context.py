"""M4: MemoryContext — theme-scoped soul-layer memory (换身体 = 换灵魂).

Locks down:
  - theme_slug sanitization (filesystem-safe, traversal-proof)
  - default theme → memory_dir root (zero migration)
  - other themes → memory_dir/themes/{slug}/
  - switch hot-swaps events/identity/mood and re-injects singletons
  - switching back restores the previous theme's memory
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.memory_context import MemoryContext, theme_slug


class TestThemeSlug:
    def test_plain(self):
        assert theme_slug("cybercat") == "cybercat"
        assert theme_slug("calico") == "calico"

    def test_normalizes(self):
        assert theme_slug("Cyber Cat!") == "cyber-cat"
        assert theme_slug("  My Theme  ") == "my-theme"

    def test_default_cases(self):
        assert theme_slug("") == "default"
        assert theme_slug(None) == "default"
        assert theme_slug("default") == "default"

    def test_traversal_sanitized(self):
        assert theme_slug("../evil") == "evil"
        assert theme_slug("..") == "default"
        assert theme_slug("a/b\\c") == "a-b-c"


@pytest.fixture
def ctx(tmp_path: Path) -> MemoryContext:
    return MemoryContext(tmp_path)


class TestSwitch:
    def test_default_uses_root(self, ctx):
        ctx.switch("default")
        ctx.event_store.add_event(title="T", content="c", weight=100)
        assert (ctx._base / "events.json").exists()

    def test_other_theme_uses_themes_subdir(self, ctx):
        ctx.switch("cybercat")
        ctx.event_store.add_event(title="T", content="c", weight=100)
        d = ctx._base / "themes" / "cybercat"
        assert d.is_dir()
        assert (d / "events.json").exists()
        # default root untouched
        assert not (ctx._base / "events.json").exists()

    def test_switch_back_restores_previous(self, ctx):
        ctx.switch("default")
        ctx.event_store.add_event(title="D1", content="c", weight=100)
        ctx.switch("cybercat")
        ctx.event_store.add_event(title="C1", content="c", weight=100)
        assert ctx.event_store.event_count() == 1

        ctx.switch("default")
        assert ctx.event_store.event_count() == 1
        assert ctx.event_store.get_all_events()[0]["title"] == "D1"

        ctx.switch("cybercat")
        assert ctx.event_store.get_all_events()[0]["title"] == "C1"

    def test_identity_and_mood_files_switch(self, ctx):
        ctx.switch("cybercat")
        assert (ctx._base / "themes" / "cybercat" / "MEMORY.md").exists() is False
        # After a write, file lands in the theme dir.
        ctx.memory_store.add("memory", "某个只有 cybercat 知道的事")
        assert (ctx._base / "themes" / "cybercat" / "MEMORY.md").exists()
        assert not (ctx._base / "MEMORY.md").exists()

    def test_unknown_theme_falls_back_to_default(self, ctx):
        slug = ctx.switch("../../../")
        assert slug == "default"
        assert ctx.current_theme == "default"

    def test_same_theme_switch_idempotent(self, ctx):
        ctx.switch("cybercat")
        ctx.event_store.add_event(title="T", content="c", weight=100)
        ctx.switch("cybercat")  # no-op reload — memory intact
        assert ctx.event_store.event_count() == 1

    def test_singletons_reinjected(self, ctx):
        from gateway.memory import recall
        from gateway.memory import tool
        ctx.switch("cybercat")
        assert recall.get_event_store() is ctx.event_store
        assert tool.get_memory_store() is ctx.memory_store
