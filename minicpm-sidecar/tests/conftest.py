"""Pytest-wide fixtures.

B-5: the gateway requires X-MiniCPM-Token on every endpoint (except
/api/health). TestClients would all 401 without the header. This conftest
patches fastapi.testclient.TestClient so every request automatically
carries the token that the gateway generated into ~/.minicpm/gateway-token
(real user home is fine for tests — the file path is deterministic).
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import testclient as _tc


def _gateway_token() -> str:
    p = Path(os.path.expanduser("~")) / ".minicpm" / "gateway-token"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


_OrigTestClient = _tc.TestClient


class _TokTestClient(_OrigTestClient):  # type: ignore[misc]
    def request(self, method, url, **kwargs):  # noqa: D102
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("x-minicpm-token", _gateway_token())
        return super().request(method, url, headers=headers, **kwargs)


_tc.TestClient = _TokTestClient
