"""Pytest-wide fixtures.

B-5: the gateway requires X-DeskPet-Token on every endpoint (except
/api/health). TestClients would all 401 without the header. This conftest
patches fastapi.testclient.TestClient so every request automatically
carries the token that the gateway generated into ~/.pet/gateway-token
(real user home is fine for tests — the file path is deterministic).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import testclient as _tc


@pytest.fixture(autouse=True)
def _isolate_memory_root(tmp_path, monkeypatch):
    """Pin every test to its own memory root.

    Since a19eb4c the dev fallback IS the live root, and several test
    files call build_app() without PET_MEMORY_DIR — so a bare
    `pytest tests/` used to write the user's real memories
    (mood.json / events.json) and run the plugin watcher over their live
    plugins directory. That is user data being mutated by a test run.
    """
    monkeypatch.setenv("PET_MEMORY_DIR", str(tmp_path / "memories"))


def _gateway_token() -> str:
    p = Path(os.path.expanduser("~")) / ".pet" / "gateway-token"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


_OrigTestClient = _tc.TestClient


class _TokTestClient(_OrigTestClient):  # type: ignore[misc]
    def request(self, method, url, **kwargs):  # noqa: D102
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("x-pet-token", _gateway_token())
        return super().request(method, url, headers=headers, **kwargs)


_tc.TestClient = _TokTestClient
