"""The persona / memory contract has exactly one owner: the gateway.

Electron keeps an inline fallback for the case where the gateway has not
answered yet. That fallback is allowed to exist, but it is not allowed to
drift — otherwise B-6 (two shells, two prompt assemblies) comes straight
back through the offline path.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from gateway import persona
from gateway import server as S
from gateway.providers import ProviderRegistry

RENDERER = (
    Path(__file__).resolve().parents[2]
    / "clawd-on-desk"
    / "src"
    / "pet-chat-renderer.js"
)


class _NativeProvider:
    name = "native-test"
    supports_streaming = True
    supports_function_calling = True
    allow_tools = True
    is_local = False


def _app_with_native_provider(monkeypatch):
    """Register a native-tool provider so the endpoint can be exercised."""
    registry = ProviderRegistry()
    registry.register(_NativeProvider())
    monkeypatch.setattr(S, "ProviderRegistry", lambda: registry)
    return S.build_app(initial_model=None)


def _electron_fallback() -> str:
    src = RENDERER.read_text(encoding="utf-8").replace("\r\n", "\n")
    match = re.search(r"ctx \+= `(\\n\\n 你是生命体[\s\S]*?)`;", src)
    assert match, "Electron offline persona fallback not found"
    # The JS template starts with the two-character escape "\n\n"; the rest
    # of the block is literal newlines. Decode just that escape form.
    return match.group(1).replace("\\n", "\n")


def test_prompt_carries_the_whole_contract():
    prompt = persona.base_prompt_block()
    for marker in (
        "你是生命体",
        "【你的记忆】",
        "记忆.md",
        "<<<MEM>>>",
        "<<<MEMEND>>>",
        "[EMOTION:xxx]",
        "[NEXT_CHAT:秒数]",
        "[NEXT_CHAT:0]",
    ):
        assert marker in prompt, marker


def test_prompt_has_no_mood_metric_or_self_report():
    prompt = persona.base_prompt_block()
    assert "情绪指数" not in prompt
    assert "你此刻的心情" not in prompt


def test_electron_fallback_is_byte_identical_to_the_gateway():
    assert _electron_fallback() == persona.base_prompt_block()


def test_native_variant_only_swaps_the_tool_marker_wording():
    default = persona.base_prompt_block()
    native = persona.base_prompt_block(native_tools=True)

    assert native != default
    assert "[MCP:builtin/recall" not in native
    assert "recall 工具" in native
    assert "原生工具调用" in native
    assert "文本标记" in native
    for marker in ("memory", "memory_search", "recall", "【你的记忆】", "<<<MEM>>>", "[EMOTION:xxx]", "[NEXT_CHAT:0]"):
        assert marker in native, marker

    # Personality / memory / emotion / NEXT_CHAT must not drift: exactly the
    # single recall sentence changes.
    default_lines = default.splitlines()
    native_lines = native.splitlines()
    assert len(default_lines) == len(native_lines)
    changed = [(a, b) for a, b in zip(default_lines, native_lines) if a != b]
    assert len(changed) == 1
    assert "[MCP:builtin/recall" in changed[0][0]
    assert "recall 工具" in changed[0][1]


def test_compact_prompt_keeps_the_load_bearing_contract():
    compact = persona.compact_prompt_block()
    full = persona.base_prompt_block()

    assert len(compact) < len(full) * 0.6
    for marker in (
        "记忆.md",
        "memory",
        "[MCP:builtin/recall",
        "<<<MEM>>>",
        "<<<MEMEND>>>",
        "[EMOTION:",
        "[NEXT_CHAT:秒数]",
        "<<<DSTATE>>>",
        "<<<DSTATEEND>>>",
    ):
        assert marker in compact, marker


def test_prompt_endpoint_serves_compact_variant():
    app = S.build_app(initial_model=None)
    with TestClient(app) as client:
        resp = client.get("/api/prompt/system", params={"provider": "deepseek-web", "compact": "1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["compact"] is True
    assert body["prompt"] == persona.compact_prompt_block()


def test_prompt_endpoint_serves_the_canonical_block():
    app = S.build_app(initial_model=None)
    with TestClient(app) as client:
        resp = client.get("/api/prompt/system")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["version"] == persona.PROMPT_VERSION
    assert body["native_tools"] is False
    assert body["prompt"] == persona.base_prompt_block()


def test_prompt_endpoint_uses_native_variant_for_native_provider(monkeypatch):
    app = _app_with_native_provider(monkeypatch)
    with TestClient(app) as client:
        resp = client.get("/api/prompt/system", params={"provider": "native-test"})
        local = client.get("/api/prompt/system", params={"provider": "local"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["native_tools"] is True
    assert body["prompt"] == persona.base_prompt_block(native_tools=True)
    assert "[MCP:builtin/recall" not in body["prompt"]

    assert local.status_code == 200
    assert local.json()["native_tools"] is False
    assert local.json()["prompt"] == persona.base_prompt_block()


def test_prompt_endpoint_unknown_provider_falls_back_to_text():
    app = S.build_app(initial_model=None)
    with TestClient(app) as client:
        resp = client.get("/api/prompt/system", params={"provider": "does-not-exist"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["native_tools"] is False
    assert body["prompt"] == persona.base_prompt_block()
