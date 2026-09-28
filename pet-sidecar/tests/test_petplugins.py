"""petplugins — cordis-inspired plugin container tests.

Core semantics under test: isolation (a broken plugin never disturbs the
others), reversibility (unload runs disposers; reload swaps cleanly),
dependency injection (missing services skip with a warning), hot reload
(mtime change swaps the plugin), and the tool surface.
"""

from __future__ import annotations

import time

import pytest

from gateway.petplugins import PluginManager


@pytest.fixture()
def pm(tmp_path):
    services = {"memory_dir": tmp_path, "events": object(), "mood": object()}
    return PluginManager(services)


def _write(pdir, name, code):
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{name}.py").write_text(code, encoding="utf-8")
    # mtime resolution: make sure a rewrite is detectable
    time.sleep(0.02)


def test_plugin_registers_tools_and_they_execute(pm, tmp_path):
    pdir = tmp_path / "plugins"
    _write(pdir, "greeter", """
def apply(ctx):
    ctx.tool("pet_hi", "say hi", None, lambda args: "hi " + str(args.get("who", "?")))
""")
    pm.sync()
    tools = pm.all_tools()
    assert "pet_hi" in tools
    import asyncio
    assert asyncio.run(pm.call_tool("pet_hi", {"who": "凌凌"})) == "hi 凌凌"


def test_hot_reload_picks_up_edits(pm, tmp_path):
    pdir = tmp_path / "plugins"
    _write(pdir, "counter", """
def apply(ctx):
    ctx.tool("pet_what", "", None, lambda args: "v1")
""")
    pm.sync()
    assert pm.all_tools()["pet_what"]["fn"]({}) == "v1"
    # NO sleep before the rewrite — same-second edits are exactly the case
    # where importlib's whole-second pycache mtime served stale bytecode.
    (pdir / "counter.py").write_text("""
def apply(ctx):
    ctx.tool("pet_what", "", None, lambda args: "v2")
""", encoding="utf-8")
    pm.sync()
    assert pm.all_tools()["pet_what"]["fn"]({}) == "v2"
    # exactly one context survived the swap (reversibility)
    assert len(pm.describe()) == 1


def test_broken_plugin_is_isolated(pm, tmp_path):
    pdir = tmp_path / "plugins"
    _write(pdir, "good", """
def apply(ctx):
    ctx.tool("pet_good", "", None, lambda args: "ok")
""")
    _write(pdir, "bad", "this is not python at all <<<")
    result = pm.sync()
    assert result["failed"] == ["bad"]
    assert "pet_good" in pm.all_tools()


def test_removed_file_unloads_plugin(pm, tmp_path):
    pdir = tmp_path / "plugins"
    _write(pdir, "temp", """
def apply(ctx):
    ctx.tool("pet_t", "", None, lambda args: "x")
""")
    pm.sync()
    assert "pet_t" in pm.all_tools()
    (pdir / "temp.py").unlink()
    pm.sync()
    assert "pet_t" not in pm.all_tools()


def test_missing_injected_service_skips_plugin(pm, tmp_path):
    pdir = tmp_path / "plugins"
    _write(pdir, "needy", """
inject = ["nonexistent_service"]

def apply(ctx):
    raise RuntimeError("should never be called")
""")
    result = pm.sync()
    assert result["skipped"] == ["needy"]
    assert result["loaded"] == [] and result["failed"] == []
    assert pm.describe() == []


def test_disposers_run_on_unload_in_reverse(pm, tmp_path):
    pdir = tmp_path / "plugins"
    marker = tmp_path / "disposer.log"
    _write(pdir, "sidefx", f'''
def apply(ctx):
    marker = r"{marker}"
    def d(tag):
        with open(marker, "a", encoding="utf-8") as f:
            f.write(tag)
    ctx._disposables.append(lambda: d("A"))
    ctx._disposables.append(lambda: d("B"))
''')
    pm.sync()
    assert marker.exists() is False
    pm._unload("sidefx")
    assert marker.read_text(encoding="utf-8") == "BA"  # LIFO: reverse registration order


def test_events_bus_connects_plugins(pm, tmp_path):
    pdir = tmp_path / "plugins"
    _write(pdir, "listener", """
SEEN = []

def apply(ctx):
    def on_ping(**kw):
        SEEN.append(kw.get("v"))
    ctx.on("ping", on_ping)
    ctx.tool("pet_seen", "", None, lambda args: repr(SEEN))
""")
    pm.sync()
    pm.emit("ping", v=42)
    import asyncio
    assert asyncio.run(pm.call_tool("pet_seen", {})) == "[42]"


def test_seed_example_plugin_loads(pm, tmp_path):
    # sync creates the dir + seeds self_note.py; it must load cleanly
    result = pm.sync()
    assert (tmp_path / "plugins" / "self_note.py").exists()
    assert "self_note" in result["loaded"]
    assert "pet_self_note_add" in pm.all_tools()
    # tools actually work
    import asyncio
    out = asyncio.run(pm.call_tool("pet_self_note_add", {"text": "今天天气好"}))
    assert "记下了" in out


def test_protected_core_context_survives_sync(pm, tmp_path):
    """plugin_forge is registered WITHOUT a file — the hot-reload scan
    must never mistake it for a removed plugin (this actually happened)."""
    from gateway.petplugins import PluginContext
    ctx = PluginContext("plugin_forge", pm)
    ctx.tool("pet_forge_plugin", "", None, lambda args: "x")
    pm._contexts["plugin_forge"] = ctx
    pm.protect("plugin_forge")
    pm.sync()  # scans; no file named plugin_forge.py exists
    assert "pet_forge_plugin" in pm.all_tools()
