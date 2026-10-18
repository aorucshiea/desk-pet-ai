"""Self-authored intuitions — pet_add_intuition / list / remove.

The code path is a policy gate (plugin_policy AST + tiny builtins), not a
process sandbox. These tests pin the gate and the persistence round-trip.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway import server as server_mod
from gateway.decision.registry import IntuitionRegistry

SRC = "def intuition(state):\n    return 0.9 if '代码' in state.get('last_user_msg', '') else 0.1\n"


def test_add_custom_and_persist(tmp_path):
    reg = IntuitionRegistry()
    result = reg.add_custom("code_curiosity", "用户聊代码时想参与", SRC)

    assert result["status"] == "ok"
    assert reg.get("code_curiosity").scorer({"last_user_msg": "这段代码怎么改"}) == 0.9

    path = tmp_path / "intuitions.json"
    assert reg.save_custom(path) is True
    again = IntuitionRegistry()
    assert again.load_custom(path) == 1
    assert again.get("code_curiosity").scorer({"last_user_msg": "代码"}) == 0.9


@pytest.mark.parametrize("bad", [
    "import os\ndef intuition(state):\n    return 0.5\n",
    "def intuition(state):\n    return open('x').read()\n",
    "def intuition(state):\n    return eval('1')\n",
    "def nope(state):\n    return 0.5\n",
])
def test_rejects_dangerous_or_invalid_code(bad):
    reg = IntuitionRegistry()
    result = reg.add_custom("bad", "x", bad)
    assert result["status"] == "error"


def test_custom_limit_is_16():
    reg = IntuitionRegistry()
    for i in range(16):
        assert reg.add_custom(f"i{i}", "", SRC)["status"] == "ok"
    assert reg.add_custom("i16", "", SRC)["status"] == "error"


def test_builtins_cannot_be_removed():
    reg = IntuitionRegistry()
    assert reg.remove("emotion")["status"] == "error"
    assert reg.remove("nope")["status"] == "error"


def test_tool_roundtrip(tmp_path, monkeypatch):
    reg = IntuitionRegistry()
    monkeypatch.setattr(server_mod, "_intuition_registry_ref", reg)
    monkeypatch.setattr(server_mod, "_ephemeral_ref", SimpleNamespace(memory_dir=tmp_path))

    out = server_mod._intuition_tool("pet_add_intuition", {
        "name": "code_curiosity",
        "description": "用户聊代码时想参与",
        "code": SRC,
    })
    assert "已注册" in out
    assert (tmp_path / "intuitions.json").exists()

    listing = server_mod._intuition_tool("pet_list_intuitions", {})
    assert "code_curiosity" in listing

    out = server_mod._intuition_tool("pet_remove_intuition", {"name": "code_curiosity"})
    assert "已删除" in out
    assert reg.get("code_curiosity") is None
