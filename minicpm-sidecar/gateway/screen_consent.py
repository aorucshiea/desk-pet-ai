"""Per-action screen permission (AI-agent style) for the desk pet.

Three states (mirrors the legacy consent enum):
  - "always": full permission — tools run without asking.
  - "once":   promoted to "always" on first use (tool calls are their
              own evidence of intent).
  - "deny":   every screen action posts a permission request to the
              Electron host (which shows an Allow/Deny/Always dialog)
              and AWAITS the user's decision. Timeout → denied.

The manager is a plain class so the gateway can embed one instance and
tests can exercise request/respond/timeout directly.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, Optional

VALID_STATES = ("deny", "once", "always")

DEFAULT_TIMEOUT_SECONDS = 30.0


class ScreenPermissionManager:
    def __init__(self, initial: str = "deny") -> None:
        self.state: str = initial if initial in VALID_STATES else "deny"
        self._lock = asyncio.Lock()
        self._pending: Dict[str, asyncio.Future] = {}
        self._counter = 0

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def request(
        self,
        notify: Callable[[str, str, str], None],
        tool_name: str,
        description: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> bool:
        """Ask the user to authorize one screen action.

        ``notify(request_id, tool_name, description)`` tells the Electron
        host to show the authorization dialog. Returns True when allowed,
        False on user denial or timeout.
        """
        # CRITICAL: the lock must only guard state read + request
        # registration. Awaiting the user's decision while holding it
        # deadlocks with respond() (which needs the same lock).
        fut: Optional[asyncio.Future] = None
        request_id: Optional[str] = None
        async with self._lock:
            if self.state == "always":
                return True
            if self.state == "deny":
                self._counter += 1
                request_id = f"perm_{self._counter}"
                fut = asyncio.get_running_loop().create_future()
                self._pending[request_id] = fut
            else:
                # "once" → promote so tool calls don't exhaust consent.
                self.state = "always"
                return True
        try:
            notify(request_id, tool_name, description)
        finally:
            pass
        try:
            return bool(await asyncio.wait_for(fut, timeout))
        except asyncio.TimeoutError:
            return False
        finally:
            self._pending.pop(request_id, None)

    async def respond(self, request_id: str, allow: bool, remember: bool = False) -> dict:
        """Electron host's reply to a pending permission request."""
        fut = self._pending.get(request_id)
        if fut is None or fut.done():
            return {"ok": False, "error": "unknown or expired request"}
        if remember:
            async with self._lock:
                self.state = "always"
        if not fut.done():
            fut.set_result(bool(allow))
        return {"ok": True, "allowed": bool(allow)}
