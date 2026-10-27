"""Best-effort bridge that pushes pet states to a running clawd-on-desk server.

The desktop pet listens on 127.0.0.1:23333-23337 and writes the active port to
~/.clawd/runtime.json. We just POST to /state and silently drop the event if
the pet is not running.

Ported verbatim from the legacy pet-pet-bridge so the pet state mapping
(thinking / working / attention / etc.) keeps the same semantics under the
new llama.cpp backend.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx

CLAWD_PORTS = [23333, 23334, 23335, 23336, 23337]
RUNTIME_FILE = Path.home() / ".clawd" / "runtime.json"
# B-5 hardening made the clawd server require the shared secret
# (x-clawd-token, ~/.clawd/server-token). Without this header every
# gateway state push was rejected with `reason: token` — the pet never
# saw thinking/working/idle from the chat loop (only hook-driven states
# worked). The Electron hook client (hooks/server-config.js
# withAuthHeader) sends this header on every candidate port; do the same.
TOKEN_FILE = Path.home() / ".clawd" / "server-token"
TOKEN_HEADER = "x-clawd-token"


def _read_clawd_token() -> str:
    try:
        return TOKEN_FILE.read_text("utf-8").strip()
    except Exception:
        return ""

# We borrow the claude-code agent_id so the existing animation map kicks in
# without modifying the desktop pet's agent registry.
AGENT_ID = "claude-code"


def _candidate_ports() -> list[int]:
    """Try the runtime port first (whatever is alive right now), then the rest."""
    ports: list[int] = []
    try:
        data = json.loads(RUNTIME_FILE.read_text("utf-8"))
        port = int(data.get("port") or 0)
        if port in CLAWD_PORTS:
            ports.append(port)
    except Exception:
        pass
    for p in CLAWD_PORTS:
        if p not in ports:
            ports.append(p)
    return ports


class ClawdBridge:
    """Thread-friendly state pusher with one persistent session_id per chat turn."""

    FAILURE_THRESHOLD = 3
    SUCCESS_GRACE_SECONDS = 15 * 60.0

    def __init__(self, *, enabled: bool = True, debug: bool = False) -> None:
        self.enabled = enabled
        self.debug = debug
        self._lock = threading.Lock()
        self._session_id = f"pet-{uuid.uuid4().hex[:8]}"
        self._port: Optional[int] = None
        self._cwd = os.getcwd()
        self._client = httpx.Client(timeout=0.4)
        self._token = ""
        self._last_attempt = 0.0
        self._last_success = 0.0
        self._consecutive_failures = 0

    def new_session(self) -> None:
        with self._lock:
            self._session_id = f"pet-{uuid.uuid4().hex[:8]}"

    def is_reachable(self) -> bool:
        """Conservative health, not a probe: standalone gateways stay healthy.

        Until a pet has answered, absence is normal. After a confirmed success,
        only repeated failed push rounds with no recent success mean unreachable.
        """
        if not self.enabled:
            return True
        with self._lock:
            if not self._last_attempt or not self._last_success:
                return True
            if self._consecutive_failures < self.FAILURE_THRESHOLD:
                return True
            return time.time() - self._last_success <= self.SUCCESS_GRACE_SECONDS

    def _record_attempt(self) -> None:
        with self._lock:
            self._last_attempt = time.time()

    def _record_success(self, port: int) -> None:
        with self._lock:
            self._port = port
            self._last_success = time.time()
            self._consecutive_failures = 0

    def _record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1

    def post(self, state: str, *, event: Optional[str] = None, title: Optional[str] = None, emotion: Optional[str] = None, extra: Optional[dict] = None) -> None:
        if not self.enabled:
            return
        self._record_attempt()
        body = {
            "state": state,
            "session_id": self._session_id,
            "agent_id": AGENT_ID,
            "event": event or _default_event_for(state),
            "cwd": self._cwd,
        }
        if title:
            body["session_title"] = title
        if emotion and emotion != "neutral":
            body["emotion"] = emotion
        if extra:
            body.update(extra)

        order: list[int] = []
        if self._port:
            order.append(self._port)
        for p in _candidate_ports():
            if p not in order:
                order.append(p)

        payload = json.dumps(body).encode("utf-8")
        for port in order:
            if not self._token:
                # lazy-load; the file is (re)written by the Electron boot
                self._token = _read_clawd_token()
            for attempt in (1, 2):
                try:
                    resp = self._client.post(
                        f"http://127.0.0.1:{port}/state",
                        content=payload,
                        headers={
                            "Content-Type": "application/json",
                            TOKEN_HEADER: self._token,
                        },
                    )
                    # Claim a port ONLY when it's really us: the
                    # x-clawd-server marker, or a 2xx (the desk-pet HTTP
                    # server answers known /state routes with 204/200).
                    # Anything else — Cherry Studio's Express 404, an
                    # unrelated service's 405, a 400 from an unknown state —
                    # is NOT our pet; keep probing the other ports.
                    if resp.headers.get("x-clawd-server") == "clawd-on-desk" or 200 <= resp.status_code < 300:
                        self._record_success(port)
                        if self.debug:
                            print(f"[clawd] {state} -> :{port} ({resp.status_code})")
                        return
                    if resp.status_code == 401 and attempt == 1:
                        # Stale token (the pet rewrote the file after we
                        # cached it): re-read once and retry this port.
                        fresh = _read_clawd_token()
                        if fresh and fresh != self._token:
                            self._token = fresh
                            continue
                except Exception as exc:
                    if self.debug:
                        print(f"[clawd] :{port} failed: {exc}")
                    break
            continue
        self._record_failure()
        if self.debug:
            print(f"[clawd] no server reachable; dropped state={state}")

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


def _default_event_for(state: str) -> str:
    return {
        "idle": "SessionStart",
        "thinking": "UserPromptSubmit",
        "working": "PreToolUse",
        "attention": "Stop",
        "notification": "Notification",
        "error": "StopFailure",
        "sleeping": "SessionEnd",
    }.get(state, "Notification")
