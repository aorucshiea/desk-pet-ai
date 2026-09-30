"""Reachability tests for the plugin kernel.

Every bug in the plugin-migration round was of one family: the fix (or the
feature) lived in code that the real install never executed. A dict-shaped
organ service, a closure-scoped helper, a call site behind a guard that no
existing install satisfies, a second size listener that overwrote the first.
Function-level tests were green for all of them.

These tests pin the paths that only exist in a real install:

- seeding into a plugins dir that already has content (the shape every
  upgrading user is in);
- the module-level accessors (_kernel / _organ_service) with and without a
  booted container, i.e. the degradation the chat stream depends on;
- the model's tool table, which is where "the organ is off => the model
  really lost the ability" has to show up.
"""

import asyncio

import pytest

from gateway import server as server_mod
from gateway.petplugins import PluginManager


@pytest.fixture()
def no_container():
    server_mod._plugin_container_ref.clear()
    yield
    server_mod._plugin_container_ref.clear()


class _FakeMcp:
    async def list_all_tools(self):
        return []


class _Req:
    tools_enabled = True


def _gather(mcp, req):
    return asyncio.run(server_mod._gather_tools(mcp, req))


def test_organs_reach_an_install_whose_plugins_dir_already_has_content(tmp_path):
    """The shape every upgrading user is in: the dir exists and is non-empty.

    seed_core_plugins and _seed_example both skip files that exist, but they
    must still DELIVER the ones that are missing — a guard that only runs on
    a virgin directory is how mood_watch silently never shipped.
    """
    pdir = PluginManager.plugin_dir(tmp_path)
    pdir.mkdir(parents=True)
    (pdir / "a_pre_existing_plugin.py").write_text(
        "inject = []\ndef apply(ctx):\n    pass\n", encoding="utf-8")

    mgr = PluginManager({"memory_dir": tmp_path})
    result = mgr.sync()

    for organ in ("organ_impulse", "organ_recall", "organ_decay", "organ_dream",
                  "organ_loader", "organ_continuity", "organ_resonance", "organ_somatic"):
        assert (pdir / f"{organ}.py").exists(), f"{organ} never delivered to an existing install"
    assert (pdir / "self_note.py").exists()
    assert (pdir / "mood_watch.py").exists()
    assert (pdir / "a_pre_existing_plugin.py").exists(), "pre-existing plugin must survive seeding"
    assert "organ_recall" in result["loaded"]
    mgr.shutdown()


def test_seeding_never_clobbers_an_edited_organ(tmp_path):
    pdir = PluginManager.plugin_dir(tmp_path)
    pdir.mkdir(parents=True)
    edited = "inject = []\ndef apply(ctx):\n    # model edited me\n    pass\n"
    (pdir / "organ_decay.py").write_text(edited, encoding="utf-8")

    PluginManager({"memory_dir": tmp_path}).sync()
    assert (pdir / "organ_decay.py").read_text(encoding="utf-8") == edited


def test_module_accessors_degrade_without_a_booted_container(no_container):
    """Before build_app mirrors the container, every module-level read of the
    plugin world must degrade, not NameError. _gather_tools used to raise here
    and the exception was swallowed into a debug log, so the model silently
    saw zero plugin tools for a week."""
    assert server_mod._kernel() is None
    assert server_mod._organ_service("impulse", object()) is not None

    sentinel = object()
    assert server_mod._organ_service("impulse", sentinel) is sentinel

    defs = _gather(_FakeMcp(), _Req())
    assert defs is None, "no container => no plugin tools, and no exception"


def test_gather_tools_exposes_plugin_tools_once_the_container_is_mirrored(tmp_path, no_container):
    mgr = PluginManager({"memory_dir": tmp_path})
    mgr.sync()
    server_mod._plugin_container_ref["mgr"] = mgr
    try:
        defs = _gather(_FakeMcp(), _Req())
        names = {d.name for d in defs}
        assert "recall" in names, "organ_recall's tool must reach the model's tool table"
        assert all(d.server_name == "pet" for d in defs if d.name == "recall")
    finally:
        server_mod._plugin_container_ref.clear()
        mgr.shutdown()


def test_muting_an_organ_really_removes_its_tool_from_the_model(tmp_path, no_container):
    """P2g's promise: switching an organ off must make the ability disappear
    from the model's side, not merely swap implementations."""
    mgr = PluginManager({"memory_dir": tmp_path})
    mgr.sync()
    server_mod._plugin_container_ref["mgr"] = mgr
    try:
        assert "recall" in {d.name for d in _gather(_FakeMcp(), _Req())}
        mgr.unload_by_name("organ_recall")
        assert "recall" not in {d.name for d in _gather(_FakeMcp(), _Req())}
        # and the degradation target takes over for service-style callers
        fallback = object()
        assert server_mod._organ_service("recall", fallback) is fallback
        mgr.load_by_name("organ_recall")
        assert "recall" in {d.name for d in _gather(_FakeMcp(), _Req())}
    finally:
        server_mod._plugin_container_ref.clear()
        mgr.shutdown()
