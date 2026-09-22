"""Regression test: /api/debug/chat must forward mood_store and lora_arr
to _blocking_chat_provider with positional args aligned.

The original bug passed `lora_arr` into the `mood_store` slot (the call
site forgot mood_store entirely), so every debug chat hit
`AttributeError: 'list' object has no attribute 'emotion_index'` in the
temperature modulation and the LingLing memory injection was silently
swallowed by the surrounding except.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from gateway import server as server_mod
from gateway.memory.mood import MoodStore


@pytest.fixture
def memory_dir(tmp_path, monkeypatch):
    d = tmp_path / "memories"
    d.mkdir()
    monkeypatch.setenv("MINICPM_MEMORY_DIR", str(d))
    return d


@pytest.fixture
def model_path(tmp_path):
    p = tmp_path / "model.gguf"
    p.write_bytes(b"x")
    return p


@pytest.fixture
def app_with_captured_blocking(memory_dir, model_path):
    """Build the app with LlamaServer mocked out and
    _blocking_chat_provider replaced by a stub that records its kwargs."""
    captured: dict[str, Any] = {}

    async def fake_blocking(registry, mcp_manager, bridge, req, server,
                            server_state, mood_store=None, lora_arr=None):
        captured["mood_store"] = mood_store
        captured["lora_arr"] = lora_arr
        return {"reply": "hi"}

    with patch.object(server_mod, "LlamaServer") as MockLlama, \
            patch.object(server_mod, "_blocking_chat_provider", fake_blocking):
        instance = MockLlama.return_value
        instance.start = AsyncMock()
        instance.stop = AsyncMock()
        instance.health = AsyncMock(return_value={"ok": True})
        instance.model_path = model_path
        instance.port = 12345
        instance.alive = True
        instance.adapter_paths = []

        app = server_mod.build_app(initial_model=model_path)
        yield app, captured


def test_debug_chat_passes_mood_store(app_with_captured_blocking):
    app, captured = app_with_captured_blocking
    with TestClient(app) as client:
        r = client.post("/api/debug/chat", json={"message": "hello"})
        assert r.status_code == 200
        assert r.json()["reply"] == "hi"

        # mood_store must be the gateway's MoodStore — never the lora
        # array (the old bug) — and lora_arr must be a list.
        assert isinstance(captured["mood_store"], MoodStore)
        assert isinstance(captured["lora_arr"], list)
