"""Extraction idempotency tests — 死亡遗嘱 wiring.

The same reply legitimately reaches extraction twice (gateway-side
death-note hook first, then the renderer's POST). Both the shared
apply core (normalized-text dedup) and per-event add (same content
within window) must treat the replay as a no-op — memories must never
double-count just because the client died and retried.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from gateway import server as server_mod
from gateway.memory.events import EventStore, promote_core


# ── unit: per-event dedup ────────────────────────────────────────────

def test_add_event_dedupes_same_content(tmp_path):
    store = EventStore()
    store.load_from_disk(tmp_path / "mem")
    a = store.add_event(title="加班", content="加班到凌晨（疲惫）", weight=300)
    b = store.add_event(title="加班", content="加班到凌晨（疲惫）", weight=300)
    assert a["id"] == b["id"]
    assert store.event_count() == 1


def test_add_event_keeps_genuinely_different(tmp_path):
    store = EventStore()
    store.load_from_disk(tmp_path / "mem")
    store.add_event(title="加班", content="加班到凌晨（疲惫）", weight=300)
    store.add_event(title="散步", content="晚饭后去散步（开心）", weight=200)
    assert store.event_count() == 2


# ── unit: core promotion (shared by extract + dream) ─────────────────

def test_promote_core_manual_first_then_auto(tmp_path):
    store = EventStore()
    store.load_from_disk(tmp_path / "mem")
    store.add_event(title="手动提", content="模型选的（开心）", weight=100)
    store.add_event(title="自动提", content="权重够高（平静）", weight=850)
    store.add_event(title="普通", content="普通事件（平淡）", weight=100)

    promoted = promote_core(store, ["手动提"])
    assert promoted == 2  # manual pick + auto ≥800
    flags = {e["title"]: e["core"] for e in store.get_all_events()}
    assert flags == {"手动提": True, "自动提": True, "普通": False}


def test_promote_core_respects_cap(tmp_path):
    store = EventStore()
    store.load_from_disk(tmp_path / "mem")
    for i in range(9):
        store.add_event(title=f"大事件{i}", content=f"改变关系的事{i}（平静）", weight=900)
    assert promote_core(store, []) == 7  # CORE_CAP
    assert promote_core(store, []) == 0  # bank full


# ── endpoint: shared apply core is idempotent ────────────────────────

MEM_BLOCK = (
    "好的，我记住了。[EMOTION:happy]\n<<<MEM>>>"
    '{"events":[{"title":"测试事件","content":"一起测试（开心）","weight":300}],'
    '"core":["测试事件"],"mood":{"mood":"开心","intensity":50,"reason":"测试","changed":true}}'
    "<<<MEMEND>>>"
)


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICPM_MEMORY_DIR", str(tmp_path / "memories"))
    model_path = tmp_path / "model.gguf"
    model_path.write_bytes(b"x")
    with patch.object(server_mod, "LlamaServer") as MockLlama:
        instance = MockLlama.return_value
        instance.start = AsyncMock()
        instance.stop = AsyncMock()
        instance.health = AsyncMock(return_value={"ok": True})
        instance.model_path = model_path
        instance.port = 12345
        instance.alive = True
        instance.adapter_paths = []
        app = server_mod.build_app(initial_model=model_path)
        with TestClient(app) as client:
            yield client, tmp_path / "memories"


def test_extract_endpoint_idempotent(app_client):
    client, mem_dir = app_client

    r1 = client.post("/api/events/extract", json={"response_text": MEM_BLOCK})
    assert r1.status_code == 200
    d1 = r1.json()
    assert len(d1["events_added"]) == 1
    assert d1["mood_updated"] is True

    # Same reply again (renderer POST after the death-note hook already
    # applied it) → duplicate flag, nothing re-added.
    r2 = client.post("/api/events/extract", json={"response_text": MEM_BLOCK})
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["duplicate"] is True
    assert d2["events_added"] == []

    # Exactly one event landed, and it was core-promoted.
    r3 = client.get("/api/events/list")
    events = r3.json()["events"]
    assert len(events) == 1
    assert events[0]["title"] == "测试事件"
    assert events[0]["core"] is True


def test_death_note_hook_set_by_build_app(app_client):
    client, mem_dir = app_client
    from gateway.memory.mood import MoodStore

    assert callable(server_mod._extraction_hook)
    # The hook applies to a fresh reply text (simulating a client that
    # died after the model emitted the block but before it could POST).
    fresh = MEM_BLOCK.replace("一起测试", "临终测试")
    result = server_mod._extraction_hook(fresh)
    assert result["ok"] is True and len(result["events_added"]) == 1
