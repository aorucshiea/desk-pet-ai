from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from gateway import server as server_mod
from gateway.telemetry import Telemetry


def test_counters_gauges_rates_and_snapshot_structure():
    telemetry = Telemetry()
    telemetry.inc("recall.attempts", 2)
    telemetry.inc("recall.success")
    telemetry.inc("resonance.calls", 4)
    telemetry.inc("resonance.hits", 2)
    telemetry.inc("memory.extraction.turns", 4)
    telemetry.inc("memory.extraction.missing_block")
    telemetry.inc("decision.ticks", 2)
    telemetry.inc("decision.voices")
    telemetry.gauge("memory.directory_lines", 7)

    snapshot = telemetry.snapshot()

    assert {
        "ok", "started_at", "uptime_seconds", "counters", "gauges",
        "timings", "rates",
    } <= snapshot.keys()
    assert snapshot["ok"] is True
    assert snapshot["counters"]["recall.attempts"] == 2
    assert snapshot["gauges"]["memory.directory_lines"] == 7
    assert snapshot["rates"] == {
        "recall_success": 0.5,
        "resonance_hit": 0.5,
        "mem_block_present": 0.75,
        "decision_voice_per_tick": 0.5,
    }


def test_empty_snapshot_has_null_rates():
    snapshot = Telemetry().snapshot()

    assert snapshot["counters"] == {}
    assert snapshot["gauges"] == {}
    assert snapshot["timings"] == {}
    assert set(snapshot["rates"].values()) == {None}


def test_observe_records_aggregate_and_recent_timing():
    telemetry = Telemetry()
    for value in (0.1, 0.2, 0.3):
        telemetry.observe("chat.first_token_seconds", value)

    snapshot = telemetry.snapshot()
    timing = snapshot["timings"]["chat.first_token_seconds"]

    assert timing["count"] == 3
    assert timing["total_seconds"] == pytest.approx(0.6)
    assert timing["avg_seconds"] == pytest.approx(0.2)
    assert timing["last_seconds"] == pytest.approx(0.3)
    assert timing["p95_recent_seconds"] == pytest.approx(0.3)
    assert snapshot["counters"]["chat.first_token_seconds.count"] == 3


def test_attach_flush_restores_counters_and_gauges(tmp_path):
    first = Telemetry()
    first.attach(tmp_path)
    first.inc("chat.turns", 2)
    first.gauge("chat.prompt_tokens", 123)
    first.observe("chat.turn_seconds", 0.25)
    first.flush()

    saved = json.loads((tmp_path / "telemetry.json").read_text(encoding="utf-8"))
    assert saved["counters"]["chat.turns"] == 2
    assert saved["gauges"]["chat.prompt_tokens"] == 123

    second = Telemetry()
    second.attach(tmp_path)
    snapshot = second.snapshot()
    assert snapshot["counters"]["chat.turns"] == 2
    assert snapshot["counters"]["chat.turn_seconds.count"] == 1
    assert snapshot["counters"]["chat.turn_seconds.total_seconds"] == 0.25
    assert snapshot["gauges"]["chat.prompt_tokens"] == 123
    assert snapshot["gauges"]["chat.turn_seconds.last_seconds"] == 0.25


class _Provider:
    name = "local"
    supports_function_calling = False

    def __init__(self, events):
        self.events = events

    async def chat(self, **_kwargs):
        for event in self.events:
            yield event


class _Mood:
    emotion_index = 0.0

    def format_for_system_prompt(self):
        return ""

    def apply_emotion_tag(self, _tag):
        pass


class _Bridge:
    def new_session(self):
        pass

    def post(self, *_args, **_kwargs):
        pass


def _drain_chat(monkeypatch, events):
    provider = _Provider(events)
    monkeypatch.setattr(server_mod, "_resolve_provider", lambda *_a, **_k: provider)
    monkeypatch.setattr(server_mod, "get_event_store", lambda: None)
    monkeypatch.setattr(server_mod, "_kernel", lambda: None)
    monkeypatch.setattr(server_mod, "_extraction_hook", None)
    req = server_mod.ChatRequest(
        messages=[{"role": "user", "content": "你好"}],
        tools_enabled=False,
        silent=True,
    )

    async def drain():
        return [
            chunk
            async for chunk in server_mod._stream_chat_provider(
                object(), object(), _Bridge(), req, None, {}, _Mood()
            )
        ]

    return asyncio.run(drain())


def test_chat_turn_first_token_prompt_and_duration_metrics(monkeypatch):
    telemetry = Telemetry()
    monkeypatch.setattr(server_mod.telemetry, "_instance", telemetry)

    chunks = _drain_chat(monkeypatch, [
        {"type": "think", "content": "想了想"},
        {"type": "delta", "content": "你好"},
        {"type": "end"},
    ])

    assert any(b'"event": "end"' in chunk for chunk in chunks)
    snapshot = telemetry.snapshot()
    assert snapshot["counters"]["chat.turns"] == 1
    assert snapshot["timings"]["chat.first_token_seconds"]["count"] == 1
    assert snapshot["timings"]["chat.turn_seconds"]["count"] == 1
    assert snapshot["gauges"]["chat.prompt_tokens"] > 0
    assert snapshot["gauges"]["chat.tools"] == 0


def test_chat_error_does_not_record_successful_turn_duration(monkeypatch):
    telemetry = Telemetry()
    monkeypatch.setattr(server_mod.telemetry, "_instance", telemetry)

    _drain_chat(monkeypatch, [{"type": "error", "message": "boom"}])

    snapshot = telemetry.snapshot()
    assert snapshot["counters"]["chat.turns"] == 1
    assert snapshot["counters"]["chat.errors"] == 1
    assert "chat.turn_seconds" not in snapshot["timings"]


class _HealthyBridge:
    def __init__(self, *_args, **_kwargs):
        pass

    def post(self, *_args, **_kwargs):
        pass

    def is_reachable(self):
        return True

    def close(self):
        pass


def test_memory_stats_endpoint_and_extraction_metrics(tmp_path, monkeypatch):
    telemetry = Telemetry()
    monkeypatch.setattr(server_mod.telemetry, "_instance", telemetry)
    monkeypatch.setattr(server_mod, "ClawdBridge", _HealthyBridge)
    monkeypatch.setenv("PET_THEME", "default")
    for name in ("_ephemeral_ref", "_intuition_registry_ref", "_body_log_ref", "_decision_loop_ref"):
        monkeypatch.setattr(server_mod, name, None)

    app = server_mod.build_app(initial_model=None)
    with TestClient(app) as client:
        initial = client.get("/api/memory/stats")
        assert initial.status_code == 200
        body = initial.json()
        assert body["memory"] == {
            "theme": "default", "event_count": 0, "total_weight": 0, "core_count": 0,
        }
        assert body["mood"]["current_mood"] == "平静"
        assert body["decision"]["initialized"] is True
        assert isinstance(body["decision"]["ticks"], int)
        assert "last_error" in body["decision"]

        reply = (
            "记住了<<<MEM>>>"
            '{"events":[{"title":" telemetry ","content":"一起测试（开心）","weight":300}],'
            '"core":["telemetry"],'
            '"mood":{"mood":"开心","intensity":55,"reason":"测试","changed":true}}'
            "<<<MEMEND>>>"
        )
        assert client.post("/api/events/extract", json={"response_text": reply}).status_code == 200
        assert client.post("/api/events/extract", json={"response_text": reply}).json()["duplicate"] is True
        assert client.post("/api/events/extract", json={"response_text": "没有记忆块"}).status_code == 200
        assert client.get("/api/events/context").status_code == 200

        body = client.get("/api/memory/stats").json()
        assert body["memory"]["event_count"] == 1
        assert body["memory"]["total_weight"] >= 300
        assert body["memory"]["core_count"] == 1
        assert body["mood"]["current_mood"] == "开心"
        assert body["mood"]["intensity"] == 55
        assert "emotion_index" in body["mood"]
        assert body["counters"]["memory.extraction.turns"] == 2
        assert body["counters"]["memory.extraction.duplicates"] == 1
        assert body["counters"]["memory.extraction.missing_block"] == 1
        assert body["counters"]["memory.extraction.events"] == 1
        assert body["rates"]["mem_block_present"] == 0.5
        assert body["gauges"]["memory.events"] == 1
        assert body["gauges"]["memory.directory_lines"] == 0
        assert body["gauges"]["homeostasis.degraded"] == 0

    assert (tmp_path / "memories" / "telemetry.json").is_file()
