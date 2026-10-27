"""ClawdBridge state-push auth tests.

B-5 hardening made the clawd server require `x-clawd-token`
(~/.clawd/server-token). The bridge used to send no token at all, so every
gateway-driven pet state (thinking/working/idle from the chat loop) was
rejected with `reason: token` — only hook-driven states ever worked.
These tests pin the contract: token header always present, stale-token
401 recovers by re-reading the file.
"""

from __future__ import annotations

import threading

from gateway.clawd_state import ClawdBridge, _read_clawd_token

TOKEN = "tok-stable"
TOKEN_FRESH = "tok-regenerated"


class _Resp:
    def __init__(self, status_code: int, headers: dict | None = None):
        self.status_code = status_code
        self.headers = headers or {}


class _Client:
    """Records posts per port; scripts the responses."""

    def __init__(self, script):
        # script: {port: [response_or_exception per post]}
        self.script = script
        self.posts = []  # (port, headers)

    def post(self, url, content=None, headers=None):
        port = int(url.rsplit(":", 1)[1].split("/")[0])
        self.posts.append((port, headers or {}))
        queue = self.script.get(port)
        if not queue:
            return _Resp(404)
        resp = queue.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def _bridge(client, monkeypatch, token=TOKEN):
    monkeypatch.setattr("gateway.clawd_state._read_clawd_token", lambda: token)
    b = ClawdBridge()
    b._client = client
    return b


def test_post_sends_token_header(monkeypatch):
    client = _Client({23333: [_Resp(204)]})
    b = _bridge(client, monkeypatch)
    b.post("thinking")
    assert any(h.get("x-clawd-token") == TOKEN for _, h in client.posts)


def test_post_claims_port_on_2xx_and_stops_probing(monkeypatch):
    client = _Client({23333: [_Resp(204)], 23334: [_Resp(204)]})
    b = _bridge(client, monkeypatch)
    b.post("idle")
    assert b._port == 23333
    assert [p for p, _ in client.posts] == [23333]


def test_stale_token_401_recovers_by_rereading(monkeypatch):
    reads = [TOKEN, TOKEN_FRESH]

    def read():
        return reads.pop(0) if reads else TOKEN_FRESH

    monkeypatch.setattr("gateway.clawd_state._read_clawd_token", read)
    client = _Client({23333: [_Resp(401), _Resp(204)]})
    b = ClawdBridge()
    b._client = client
    b.post("working")
    assert b._port == 23333
    sent = [h.get("x-clawd-token") for _, h in client.posts]
    assert sent == [TOKEN, TOKEN_FRESH]


def test_repeated_401_without_recovery_drops_state(monkeypatch):
    client = _Client({23333: [_Resp(401), _Resp(401)]})
    b = _bridge(client, monkeypatch)
    b.post("error")
    assert b._port is None  # never claimed; state dropped, no raise


def test_exception_on_port_continues_to_next(monkeypatch):
    client = _Client({
        23333: [ConnectionError("boom")],
        23334: [_Resp(204)],
    })
    b = _bridge(client, monkeypatch)
    b.post("attention")
    assert b._port == 23334


def test_read_clawd_token_missing_file(monkeypatch):
    from pathlib import Path

    def boom(_):
        raise OSError("missing")

    monkeypatch.setattr(Path, "read_text", boom)
    assert _read_clawd_token() == ""


def test_thread_safety_smoke(monkeypatch):
    client = _Client({23333: [_Resp(204) for _ in range(40)]})
    b = _bridge(client, monkeypatch)

    def hammer():
        for _ in range(10):
            b.post("thinking")

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert client.posts, "expected posts to be recorded"
    assert all(h.get("x-clawd-token") == TOKEN for _, h in client.posts)


def test_health_treats_never_seen_pet_as_standalone_not_sick(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("gateway.clawd_state.time.time", lambda: now[0])
    client = _Client({})
    b = _bridge(client, monkeypatch)

    assert b.is_reachable() is True
    for _ in range(4):
        b.post("thinking")

    assert b._last_attempt == 1000.0
    assert b._last_success == 0.0
    assert b._consecutive_failures == 4
    assert b.is_reachable() is True


def test_health_requires_failures_and_stale_success_then_recovers(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("gateway.clawd_state.time.time", lambda: now[0])
    client = _Client({23333: [_Resp(204)]})
    b = _bridge(client, monkeypatch)

    b.post("idle")
    assert b._last_success == 1000.0
    assert b._consecutive_failures == 0

    b.post("thinking")
    assert b._consecutive_failures == 1
    assert b.is_reachable() is True

    now[0] += b.SUCCESS_GRACE_SECONDS + 1
    b.post("thinking")
    assert b._consecutive_failures == 2
    assert b.is_reachable() is True
    b.post("thinking")
    assert b._consecutive_failures == 3
    assert b.is_reachable() is False

    client.script[23333].append(_Resp(204))
    b.post("attention")
    assert b._last_success == now[0]
    assert b._consecutive_failures == 0
    assert b.is_reachable() is True


def test_disabled_bridge_does_not_record_health_failures(monkeypatch):
    b = ClawdBridge(enabled=False)
    b._client = _Client({})

    b.post("idle")

    assert b._last_attempt == 0.0
    assert b._consecutive_failures == 0
    assert b.is_reachable() is True
