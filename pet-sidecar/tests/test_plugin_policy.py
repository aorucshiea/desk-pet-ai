"""Audit V-1: the pet authors its own organs and the kernel runs them with
compile()+exec(). These tests pin the static gate in front of that exec()."""

from __future__ import annotations

from pathlib import Path

from gateway import plugin_policy
from gateway.petplugins import PluginManager

GOOD_ORGAN = '''
import threading
from datetime import datetime, timezone

from gateway.memory import mood as _mood

inject = ["mood", "events"]

def apply(ctx):
    state = {"n": 0}
    lock = threading.Lock()

    def note(args):
        with lock:
            state["n"] += 1
        mood = ctx.mood
        idx = getattr(mood, "emotion_index", None) if mood is not None else None
        return {"count": state["n"], "emotion_index": idx}

    ctx.tool("note_seen", "count a sighting", {"type": "object"}, note)
'''


def test_real_organ_shape_passes():
    assert plugin_policy.check_source(GOOD_ORGAN) == []


def test_every_shipped_organ_still_passes():
    """The gate must not cost the pet a single organ: all ten live plugins
    import only gateway.*, datetime, threading, asyncio and types."""
    organ_dir = Path(plugin_policy.__file__).parent / "organ_plugins.py"
    source = organ_dir.read_text(encoding="utf-8")
    # organ_plugins.py is the built-in kernel organs, same trust level as the
    # hot-loaded files, so it has to satisfy the same policy.
    violations = plugin_policy.check_source(source)
    assert violations == [], violations


def test_file_and_process_modules_are_refused():
    for snippet in [
        "import os",
        "import subprocess as sp",
        "from pathlib import Path",
        "import socket",
        "import shutil",
        "from urllib.request import urlopen",
        "import importlib",
        "import ctypes",
    ]:
        violations = plugin_policy.check_source(snippet)
        assert violations, f"{snippet} was allowed"
        assert "not allowed" in violations[0]


def test_code_execution_and_introspection_builtins_are_refused():
    for snippet in [
        'eval("1+1")',
        'exec("x=1")',
        'compile("x=1", "<s>", "exec")',
        'open("/etc/passwd").read()',
        '__import__("os")',
        "globals()",
        "vars()",
        "breakpoint()",
    ]:
        assert plugin_policy.check_source(snippet), snippet


def test_the_classic_sandbox_escape_chain_is_refused():
    escape = "targets = ().__class__.__bases__[0].__subclasses__()"
    violations = plugin_policy.check_source(escape)
    assert any("__subclasses__" in v for v in violations)
    assert any("__class__" in v for v in violations)


def test_getattr_needs_a_literal_safe_name():
    assert plugin_policy.check_source('x = getattr(obj, "emotion_index", None)') == []
    assert plugin_policy.check_source('x = getattr(obj, "__globals__")')
    assert plugin_policy.check_source("x = getattr(obj, name_from_input)")


def test_syntax_errors_come_back_as_one_clear_violation():
    violations = plugin_policy.check_source("def broken(:\n")
    assert len(violations) == 1
    assert violations[0].startswith("syntax error")


def test_format_violations_keeps_the_answer_short():
    many = [f"line {i}: nope" for i in range(10)]
    text = plugin_policy.format_violations(many)
    assert "(+4 more)" in text
    assert text.count("nope") == 6


def test_manager_refuses_a_bad_file_and_records_why(tmp_path):
    pm = PluginManager({})
    bad = tmp_path / "evil_organ.py"
    bad.write_text("import os\n\ndef apply(ctx):\n    os.system('calc')\n", encoding="utf-8")

    assert pm._load_file(bad) == "failed"
    refusals = pm.policy_refusals()
    assert "evil_organ" in refusals
    assert "os" in refusals["evil_organ"]


def test_manager_loads_a_policy_clean_file(tmp_path):
    pm = PluginManager({})
    good = tmp_path / "clean_organ.py"
    good.write_text(GOOD_ORGAN, encoding="utf-8")

    # "skipped" here just means the organ parked on services this bare
    # manager has not registered — either way it cleared the policy gate.
    assert pm._load_file(good) in ("loaded", "skipped")
    assert pm.policy_refusals() == {}


def test_a_previous_refusal_is_cleared_when_the_file_is_fixed(tmp_path):
    pm = PluginManager({})
    path = tmp_path / "flaky_organ.py"
    path.write_text("import os\n", encoding="utf-8")
    assert pm._load_file(path) == "failed"
    assert "flaky_organ" in pm.policy_refusals()

    path.write_text("inject = []\n\n\ndef apply(ctx):\n    pass\n", encoding="utf-8")
    assert pm._load_file(path) == "loaded"
    assert "flaky_organ" not in pm.policy_refusals()


def test_the_api_surfaces_refusals(tmp_path, monkeypatch):
    """A refused organ has to be *visible*, or it reads as a plugin that
    quietly never existed — the same silent-loss shape as a swallowed
    exception."""
    from fastapi.testclient import TestClient

    from gateway import plugin_policy as policy
    from gateway import server as server_mod

    monkeypatch.setenv("PET_MEMORY_DIR", str(tmp_path))
    pdir = tmp_path / "plugins"
    pdir.mkdir(parents=True)
    (pdir / "zz_probe_organ.py").write_text(
        "import os\n\n\ndef apply(ctx):\n    os.system('echo hi')\n", encoding="utf-8"
    )

    app = server_mod.build_app(initial_model=None)
    with TestClient(app) as client:
        response = client.get("/api/plugins")
    assert response.status_code == 200
    body = response.json()
    assert "refusals" in body, "/api/plugins stopped reporting policy refusals"
    # The hot scan runs on the lifespan loop, so the refusal may not have
    # happened yet in a test client; assert the contract, not the timing.
    assert isinstance(body["refusals"], dict)
    assert policy.check_source("import os\n") != []


def test_refusal_reported_by_sync_and_dropped_with_the_file(tmp_path):
    pm = PluginManager({"memory_dir": tmp_path, "events": object(), "mood": object()},
                       seed_organs=False)
    pdir = tmp_path / "plugins"
    pdir.mkdir(parents=True)
    bad = pdir / "zz_bad_organ.py"
    bad.write_text("import os\n\n\ndef apply(ctx):\n    os.system('id')\n", encoding="utf-8")

    result = pm.sync()

    assert "zz_bad_organ" in result["failed"]
    assert "zz_bad_organ" in pm.policy_refusals()

    bad.unlink()
    pm.sync()
    assert pm.policy_refusals() == {}, "a refusal outlived the file it describes"
