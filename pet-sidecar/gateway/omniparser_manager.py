"""Manages the OmniParser Lite subprocess lifecycle.

OmniParser runs as a separate FastAPI server (``D:\\omniparser-lite\\server.py``)
handling Florence2 + YOLO-based screen parsing. This manager handles:

- Lazy startup: starts on first ``/api/screen/observe`` call, not at gateway boot
- Health checks via ``/probe/``
- Auto-restart on crash (up to 3 attempts)
- Graceful shutdown when the gateway stops
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from typing import Optional

import httpx

from .log_setup import get_logger

_OMNIPARSER_PORT = 8000
_OMNIPARSER_DIR = os.environ.get("OMNIPARSER_DIR", "D:\\omniparser-lite")
_OMNIPARSER_PYTHON = os.environ.get(
    "OMNIPARSER_PYTHON",
    os.path.join(_OMNIPARSER_DIR, ".venv", "Scripts", "python.exe"),
)
_MAX_RESTARTS = 3
_READY_TIMEOUT = 60.0
_PROBE_INTERVAL = 1.0
_OOM_KEYWORDS = ["cuda out of memory", "outofmemory", "oom", "cuda error: out of memory"]


def _is_oom(stderr: str) -> bool:
    return any(kw in stderr.lower() for kw in _OOM_KEYWORDS)


class OmniParserManager:
    """Manages the OmniParser Lite subprocess."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._started: bool = False
        self._restarts: int = 0
        self._oom_count: int = 0
        self._lock = asyncio.Lock()
        self._log = get_logger()
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    async def start(self) -> None:
        """Start the OmniParser subprocess if not already running."""
        async with self._lock:
            if self._started and self.alive:
                return
            if not os.path.isdir(_OMNIPARSER_DIR):
                self._log.error("OmniParser directory not found: %s", _OMNIPARSER_DIR)
                raise FileNotFoundError(f"OmniParser directory not found: {_OMNIPARSER_DIR}")

            server_py = os.path.join(_OMNIPARSER_DIR, "server.py")
            if not os.path.isfile(server_py):
                self._log.error("OmniParser server.py not found: %s", server_py)
                raise FileNotFoundError(f"OmniParser server.py not found: {server_py}")

            self._log.info("Starting OmniParser (attempt %d)...", self._restarts + 1)
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "logs")
            os.makedirs(log_dir, exist_ok=True)
            omniparser_out = open(os.path.join(log_dir, "omniparser-out.log"), "ab")
            omniparser_err = open(os.path.join(log_dir, "omniparser-err.log"), "ab")
            self._proc = subprocess.Popen(
                [_OMNIPARSER_PYTHON, server_py],
                cwd=_OMNIPARSER_DIR,
                stdout=omniparser_out,
                stderr=omniparser_err,
            )
            self._started = True

    async def stop(self) -> None:
        """Stop the OmniParser subprocess gracefully."""
        async with self._lock:
            if self._proc is None:
                return
            if self.alive:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=5)
            self._proc = None
            self._started = False
            self._restarts = 0
            self._oom_count = 0

    async def wait_ready(self, timeout: float = _READY_TIMEOUT) -> bool:
        """Wait for OmniParser to be ready (via /probe/).

        Returns True if ready within the timeout, False otherwise.
        """
        client = self._get_client()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                resp = await client.get("/probe/", timeout=5.0)
                if resp.status_code == 200:
                    return True
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
                pass
            await asyncio.sleep(_PROBE_INTERVAL)
        return False

    async def ensure_ready(self) -> str:
        """Lazy-start OmniParser and wait for it to be ready.

        Returns:
            ``"ready"`` on success, or an error string describing the failure.
        """
        if self.alive:
            ready = await self.wait_ready(timeout=5.0)
            if ready:
                return "ready"

        try:
            await self.start()
        except FileNotFoundError as exc:
            return str(exc)
        except Exception as exc:
            self._log.exception("OmniParser start failed: %s", exc)
            return str(exc)

        ready = await self.wait_ready()
        if ready:
            self._restarts = 0
            return "ready"

        return "OmniParser failed to become ready within the timeout"

    async def health(self) -> dict:
        """Return the current health status of OmniParser."""
        if not self.alive:
            return {"alive": False, "restarts": self._restarts, "oom_count": self._oom_count}
        try:
            client = self._get_client()
            resp = await client.get("/probe/", timeout=5.0)
            return {
                "alive": True,
                "probe_status": resp.status_code,
                "restarts": self._restarts,
                "oom_count": self._oom_count,
            }
        except Exception:
            return {"alive": True, "probe_status": None, "restarts": self._restarts, "oom_count": self._oom_count}

    def _check_crash(self) -> Optional[str]:
        """Check if the subprocess crashed and classify the error.

        Returns:
            ``"gpu_oom"`` if the crash was GPU OOM,
            ``"crash"`` for other failures,
            ``None`` if the process is still alive.
        """
        if self.alive:
            return None
        if self._proc is None:
            return None

        try:
            _, stderr = self._proc.communicate(timeout=2)
            stderr_text = stderr.decode("utf-8", "ignore") if stderr else ""
        except subprocess.TimeoutExpired:
            self._proc.kill()
            _, stderr = self._proc.communicate(timeout=5)
            stderr_text = stderr.decode("utf-8", "ignore") if stderr else ""

        if _is_oom(stderr_text):
            self._oom_count += 1
            if self._oom_count >= 3:
                return "gpu_oom"
            self._log.warning("OmniParser OOM (count %d/3)", self._oom_count)
            return "crash"
        return "crash"

    async def ensure_alive(self) -> str:
        """Check if OmniParser is alive; lazy-start if never started, or restart
        if crashed (up to 3 attempts).

        First probes ``/probe/`` on port 8000 — if something is already serving
        there (e.g. manually started), we adopt it without starting a new subprocess.

        Returns:
            ``"ready"`` on success, ``"gpu_oom"`` if OOM limit exceeded,
            or an error string.
        """
        # Fast path: already alive + port responds
        if self.alive:
            already = await self.wait_ready(timeout=3.0)
            if already:
                self._started = True
                return "ready"

        # Adopt: something else is already serving on port 8000
        already = await self.wait_ready(timeout=3.0)
        if already:
            self._log.info("Adopting externally-started OmniParser on port %d", _OMNIPARSER_PORT)
            self._started = True
            return "ready"

        async with self._lock:
            crash_type = self._check_crash()
            if crash_type == "gpu_oom":
                self._log.error("OmniParser OOM limit reached, not restarting")
                return "gpu_oom"
            if self._started and self._restarts >= _MAX_RESTARTS:
                self._log.error("OmniParser restart limit reached (%d)", _MAX_RESTARTS)
                return "crash"

        self._restarts += 1
        try:
            await self.start()
        except Exception as exc:
            return str(exc)
        ready = await self.wait_ready()
        return "ready" if ready else "crash"

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{_OMNIPARSER_PORT}",
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        return self._client

    async def close(self) -> None:
        """Clean up resources."""
        await self.stop()
        if self._client and not self._client.is_closed:
            await self._client.aclose()
