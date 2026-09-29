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
    # seed_organs=False: these tests pin exact kernel semantics (loaded /
    # skipped lists); the built-in organs get their own coverage below.
    return PluginManager(services, seed_organs=False)


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


def test_seed_example_plugin_loads(tmp_path):
    # sync creates the dir + seeds self_note.py; it must load cleanly.
    # Needs seeding ON — the hermetic pm fixture (seed_organs=False) gets
    # no seeds of any kind, examples included.
    pm = PluginManager({"memory_dir": tmp_path, "events": object(), "mood": object()})
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


# ── Built-in organ plugins (P2: organs as seeds, not hardcoded loops) ──

def test_core_organs_seed_and_activate(tmp_path):
    """organ_decay loads once its two services exist; organ_dream parks on
    the rest of its dependencies (providers/schedule/... not registered)."""
    from gateway.petplugins import EFFECT_LISTENER, PluginManager

    services = {
        "memory_dir": tmp_path,
        "events": object(),
        "conversation_state": {"last_conversation_at": None},
    }
    pm = PluginManager(services)  # seed_organs defaults to True
    result = pm.sync()
    assert "organ_decay" in result["loaded"]
    assert "organ_dream" in result["skipped"]
    assert "organ_decay" in [c["name"] for c in pm.describe()]
    # the decay organ registered its conversation_end listener
    assert pm.effects_report()["by_kind"][EFFECT_LISTENER] >= 1
    pm.shutdown()
    assert "organ_decay" not in pm.describe()


def test_core_organs_seed_is_idempotent(tmp_path):
    """Re-seeding never clobbers a model/user edit of an organ file."""
    PluginManager({"memory_dir": tmp_path}).sync()
    pdir = PluginManager.plugin_dir(tmp_path)
    organ = pdir / "organ_decay.py"
    organ.write_text("inject = []\ndef apply(ctx):\n    pass\n", encoding="utf-8")
    PluginManager({"memory_dir": tmp_path}).sync()
    assert organ.read_text(encoding="utf-8") == "inject = []\ndef apply(ctx):\n    pass\n"


def test_examples_reach_existing_install(tmp_path):
    """An install whose plugins dir predates a plugin must still receive it.

    mood_watch shipped after self_note, but _seed_example used to sit behind
    a "virgin directory only" guard in sync(), so every real install kept
    missing it. Pins the delivery path itself, not just the seed body.
    """
    pdir = PluginManager.plugin_dir(tmp_path)
    pdir.mkdir(parents=True)
    (pdir / "a_pre_existing_plugin.py").write_text(
        "inject = []\ndef apply(ctx):\n    pass\n", encoding="utf-8")

    PluginManager({"memory_dir": tmp_path}).sync()
    assert (pdir / "self_note.py").exists(), "example never delivered to an existing install"
    assert (pdir / "mood_watch.py").exists(), "example never delivered to an existing install"
    assert (pdir / "a_pre_existing_plugin.py").exists()

    # and an edited example is still never clobbered
    (pdir / "self_note.py").write_text("# user edit\n", encoding="utf-8")
    PluginManager({"memory_dir": tmp_path}).sync()
    assert (pdir / "self_note.py").read_text(encoding="utf-8") == "# user edit\n"


def test_organ_decay_thread_ticks_and_listens(tmp_path, monkeypatch):
    """The decay organ's thread runs run_decay on its config interval and
    its conversation_end listener drives on_conversation_end."""
    import time as _time

    import gateway.memory.decay as decay_mod
    from gateway.petplugins import PluginManager

    calls, end_calls = [], []
    monkeypatch.setattr(decay_mod, "run_decay", lambda *a: calls.append(a))
    monkeypatch.setattr(decay_mod, "on_conversation_end", lambda *a: end_calls.append(a))

    state = {"last_conversation_at": "now"}
    pm = PluginManager({
        "memory_dir": tmp_path,
        "events": object(),
        "conversation_state": state,
    })
    pm.set_config_overrides({"organ_decay": {"interval_seconds": 1}})
    assert "organ_decay" in pm.sync()["loaded"]
    _time.sleep(1.4)
    assert calls, "run_decay should have ticked at least once"
    assert calls[0][1] == "now"  # the conversation_state cell is shared
    pm.emit("conversation_end")
    assert end_calls, "listener should fire on conversation_end"
    pm.shutdown()
    ticks_after = len(calls)
    _time.sleep(1.2)
    assert len(calls) == ticks_after, "disposer must stop the decay thread"


# ── Service shapes: what organs provide must match how server.py calls ──
#
# server.py accesses organ services by ATTRIBUTE (svc.compute_gap,
# svc.get_session_recall_count, svc.build_memory_context, ...). The first
# organ_impulse seed provided a plain dict, which passes a key-index test
# but blew up with AttributeError in production. This test pins the
# attribute-access contract so a shape regression cannot ship again.

def test_provided_services_match_production_access_pattern(tmp_path):
    class Ev:
        def append_event(self, *a, **k):
            return {"id": "e"}

    class Mood:
        emotion_index = 0.3

        def format_for_system_prompt(self):
            return "(mood)"

    class ConvState(dict):
        pass

    class MemCtx:
        current_theme = "default"

        def theme_dir(self, t):
            d = tmp_path / "themes" / str(t)
            d.mkdir(parents=True, exist_ok=True)
            return d

    from gateway.petplugins import PluginManager

    pm = PluginManager({
        "memory_dir": tmp_path,
        "events": Ev(),
        "mood": Mood(),
        "conversation_state": ConvState(),
        "memory_ctx": MemCtx(),
        # providers / llama_server / schedule deliberately ABSENT —
        # organ_dream stays parked, exactly like a cold boot without them.
    })
    pm.sync()

    # impulse: production calls svc.compute_gap(...) — attribute access.
    imp = pm.get_service("impulse")
    assert imp is not None, "organ_impulse must provide the impulse service"
    assert hasattr(imp, "compute_gap") and callable(imp.compute_gap), (
        "impulse service must be attribute-shaped (SimpleNamespace/module), "
        "not a plain dict"
    )
    gap = imp.compute_gap(emotion_index=0.0, proactive_streak=0, recall_count=0)
    assert isinstance(gap, int) and gap >= 180, gap

    # recall: production reads .get_session_recall_count, and the tool
    # schema must be identical to the builtin RECALL_TOOL_SCHEMA.
    rec = pm.get_service("recall")
    assert rec is not None, "organ_recall must provide the recall service"
    assert callable(getattr(rec, "get_session_recall_count", None))
    from gateway.memory.recall import RECALL_TOOL_SCHEMA as sch
    tool = pm.all_tools().get(sch["name"])
    assert tool is not None, "organ_recall must register the recall tool"
    assert tool["description"] == sch["description"]
    assert tool["parameters"] == sch["input_schema"]
    assert tool["fn"] is R_handler if (R_handler := rec.recall_tool_handler) else True

    # loader / continuity / resonance / somatic: attribute-accessible too.
    for name, attr in (
        ("loader", "build_memory_context"),
        ("continuity", "get_current_note"),
        ("resonance", "find_resonance"),
        ("somatic", "build_somatic_block"),
    ):
        svc = pm.get_service(name)
        assert svc is not None, f"{name} service missing (organ failed to load?)"
        assert callable(getattr(svc, attr, None)), f"{name}.{attr} must be attribute-accessible"
