"""Gateway token middleware coverage (audit B-8 / V-6).

The token gate is the ONLY thing between "any webpage in a browser on this
machine" and endpoints that click the user's real mouse, yet the whole
`pet-sidecar/tests/` tree had zero cases for it — and the comparison itself
was a plain `!=`. These tests pin both: which route is open, and that the
check goes through a constant-time compare.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from gateway import server as server_mod


def _client():
    return TestClient(server_mod.build_app(initial_model=None))


def _real_token() -> str:
    return (Path.home() / ".pet" / "gateway-token").read_text(encoding="utf-8").strip()


def test_health_stays_open_without_a_token():
    # Onboarding pings /api/health before the host has read the token file.
    with _client() as client:
        response = client.get("/api/health", headers={"x-pet-token": ""})
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_wrong_token_is_rejected_on_a_tool_capable_endpoint():
    with _client() as client:
        response = client.get("/api/plugins", headers={"x-pet-token": "not-the-token"})
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


def test_missing_token_is_rejected():
    # An empty header value is how "no header" reaches the middleware, since
    # the shared conftest injects the real token into every TestClient.
    with _client() as client:
        response = client.get("/api/plugins", headers={"x-pet-token": ""})
    assert response.status_code == 401


def test_correct_token_passes_the_gate():
    token = _real_token()
    assert len(token) >= 32, "the token file looks truncated"
    with _client() as client:
        response = client.get("/api/plugins", headers={"x-pet-token": token})
    assert response.status_code != 401


def test_a_token_that_only_shares_a_prefix_is_rejected():
    """The regression this replaces: `!=` returns as soon as a byte differs,
    so the old code leaked how much of a guess was right. `compare_digest`
    must reject a prefix as a plain mismatch — and a prefix-length guess must
    never be accepted."""
    token = _real_token()
    with _client() as client:
        short = client.get("/api/plugins", headers={"x-pet-token": token[: len(token) // 2]})
        one_off = client.get("/api/plugins", headers={"x-pet-token": token[:-1]})
    assert short.status_code == 401
    assert one_off.status_code == 401


def test_gate_uses_constant_time_comparison():
    import hmac
    import inspect

    source = inspect.getsource(server_mod.build_app)
    assert "hmac.compare_digest" in source, (
        "the token check went back to a plain != — audit V-6")
    assert callable(hmac.compare_digest)
