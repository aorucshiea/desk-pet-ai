"""MCP client wrapper using the official `mcp` PyPI SDK.

Manages a single MCP server connection via stdio or SSE/HTTP transport.
Supports initialize, list_tools, and call_tool operations.

Lifecycle note
---------------
The `mcp` SDK's transport context managers (`stdio_client`, `sse_client`,
`streamablehttp_client`) are `@asynccontextmanager` coroutines backed by anyio
TaskGroups. anyio requires the cancel scope they create to be entered AND
exited by the SAME asyncio task. If a context manager is entered in one task
and exited (or GC-finalized) in another, anyio raises
`RuntimeError: Attempted to exit cancel scope in a different task than it was
entered in` — which kills the gateway process.

To guarantee same-task pairing, each connection runs inside its own dedicated
background task (`_run`) that owns the full `transport → session` lifecycle for
the entire lifetime of the connection. `connect()` spawns that task and waits
for it to either report ready or fail; `disconnect()` signals the task to tear
down and awaits it. Neither `connect` nor `disconnect` ever touches the
transport/session context managers directly, so there is no task mismatch.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from ..log_setup import get_logger


# We import mcp SDK lazily so the gateway can start even if `mcp` is
# not installed (the import error surfaces only when someone tries to
# connect to an MCP server).
_MCP_AVAILABLE: Optional[bool] = None


def _check_mcp_sdk() -> bool:
    global _MCP_AVAILABLE
    if _MCP_AVAILABLE is not None:
        return _MCP_AVAILABLE
    try:
        import mcp  # noqa: F401
        from mcp.client.stdio import stdio_client  # noqa: F401
        _MCP_AVAILABLE = True
    except ImportError:
        _MCP_AVAILABLE = False
    return _MCP_AVAILABLE


# Give the connection handshake this long to complete before we declare the
# server unreachable. The handshake is `initialize()` + the protocol negotiate;
# a healthy server responds in well under a second, so this only fires for a
# genuinely hung/crashed subprocess where we'd otherwise park forever.
_CONNECT_TIMEOUT = 30.0
# How long to wait for the runner task to tear down cleanly after we signal
# stop before force-cancelling it (and the underlying subprocess with it).
_DISCONNECT_TIMEOUT = 10.0


class MCPTool:
    """Represents a tool exposed by an MCP server."""

    def __init__(self, name: str, description: str = "", input_schema: Optional[dict] = None) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema or {}

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class MCPClient:
    """Connection to a single MCP server via stdio or http/sse transport."""

    def __init__(
        self,
        name: str,
        command: str,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        transport: str = "stdio",
        url: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = env
        self.transport = transport  # "stdio", "http", or "sse"
        self.url = url
        self.headers = headers or {}
        self._log = get_logger()

        # Connection state. `_session` is only non-None while the runner task
        # is parked and the connection is live; callers must check
        # `_connected` before using it.
        self._session = None
        self._connected = False
        self._task: Optional[asyncio.Task] = None

        # Per-attempt synchronization primitives, created fresh in connect().
        self._ready: Optional[asyncio.Event] = None
        self._stop: Optional[asyncio.Event] = None
        self._connect_error: Optional[BaseException] = None

    # ── Transport selection ──────────────────────────────────────────

    def _transport_cm(self):
        """Return the SDK transport context manager for this client's config.

        The returned object is an `@asynccontextmanager`; it MUST be used with
        `async with` from inside `_run` so enter/exit happen in one task.
        """
        if self.transport in ("http", "sse") and self.url:
            if self.transport == "http":
                from mcp.client.streamable_http import streamablehttp_client
                return streamablehttp_client(url=self.url, headers=self.headers)
            from mcp.client.sse import sse_client
            return sse_client(url=self.url, headers=self.headers)

        from mcp.client.stdio import StdioServerParameters, stdio_client
        server_params = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=self.env,
        )
        return stdio_client(server_params)

    # ── Connection lifecycle ─────────────────────────────────────────

    async def connect(self) -> bool:
        """Connect to the MCP server.

        Spawns the runner task and waits until it reports ready (session
        initialized) or fails. Returns True on a live connection.
        """
        if self._connected:
            return True
        if not _check_mcp_sdk():
            self._log.error("MCP SDK not installed; run: uv pip install mcp")
            return False

        # A runner is already mid-handshake — don't start a second one; report
        # current state and let the in-flight attempt resolve.
        if self._task is not None and not self._task.done():
            return self._connected

        # Reap any previous (failed) runner before starting a new attempt.
        self._task = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._connect_error = None

        self._task = asyncio.create_task(self._run(), name=f"mcp-{self.name}")

        # Wait for either the runner to signal ready, or to die trying.
        waiter = asyncio.ensure_future(self._ready.wait())
        try:
            await asyncio.wait(
                {waiter, self._task},
                timeout=_CONNECT_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not waiter.done():
                waiter.cancel()

        if self._connected:
            return True

        # Runner did not reach ready. Surface the failure and reap the task so
        # we don't leave a zombie that later finalizes cross-task.
        if self._task is not None and not self._task.done():
            self._stop.set()
            try:
                await asyncio.wait_for(self._task, timeout=_DISCONNECT_TIMEOUT)
            except Exception:
                self._task.cancel()
                try:
                    await self._task
                except BaseException:
                    pass
        # If the runner crashed, surface it once for diagnostics.
        if self._connect_error is not None:
            self._log.debug("MCP %s connect failed: %s", self.name, self._connect_error)
        self._task = None
        return False

    async def _run(self) -> None:
        """Own the transport + session lifecycle in a single task.

        Runs until either `disconnect()` sets `_stop` or the connection breaks.
        All context managers are entered and exited here, so anyio's cancel
        scopes never cross task boundaries.
        """
        from mcp import ClientSession

        try:
            async with self._transport_cm() as streams:
                # stdio/sse yield (read, write); streamable_http may yield a
                # 3-tuple (+get_session_id). Indexing keeps us version-safe.
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    self._connected = True
                    self._ready.set()
                    if self.url:
                        self._log.info("MCP connected: %s (%s: %s)", self.name, self.transport, self.url)
                    else:
                        self._log.info(
                            "MCP connected: %s (stdio: %s %s)",
                            self.name, self.command, " ".join(self.args),
                        )
                    # Park until asked to disconnect. Keeping the `async with`
                    # blocks open here is what holds the connection alive.
                    await self._stop.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 — record anything, don't crash the loop
            was_connected = self._connected
            self._connect_error = exc
            if was_connected:
                self._log.warning("MCP %s connection lost: %s", self.name, exc)
            else:
                self._log.warning("MCP %s connect failed: %s", self.name, exc)
        finally:
            self._session = None
            self._connected = False
            # Unblock connect() if it's still waiting on _ready.
            if self._ready is not None:
                self._ready.set()

    async def disconnect(self) -> None:
        """Disconnect from the MCP server.

        Signals the runner task to tear down and awaits its clean exit. Because
        the runner owns the transport/session context managers, their teardown
        happens in the same task that created them — no cancel-scope mismatch.
        """
        task = self._task
        if task is None:
            self._session = None
            self._connected = False
            return

        if self._stop is not None:
            self._stop.set()

        if not task.done():
            try:
                await asyncio.wait_for(task, timeout=_DISCONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                # Runner didn't wind down in time (hung subprocess / stuck I/O).
                # Cancel to force the anyio TaskGroup to tear it down.
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
            except asyncio.CancelledError:
                raise
            except BaseException:
                # Swallow errors surfaced from the runner during teardown —
                # disconnect is best-effort.
                pass

        self._task = None
        self._session = None
        self._connected = False
        self._log.info("MCP disconnected: %s", self.name)

    # ── Operations (only valid while connected) ──────────────────────

    async def list_tools(self) -> list[MCPTool]:
        """List available tools from the MCP server.

        Returns empty list on failure.
        """
        if not self._connected or self._session is None:
            self._log.warning("MCP %s: not connected, cannot list tools", self.name)
            return []
        try:
            result = await self._session.list_tools()
            tools = []
            for t in getattr(result, "tools", result if isinstance(result, list) else []):
                tools.append(MCPTool(
                    name=t.name,
                    description=getattr(t, "description", "") or "",
                    input_schema=getattr(t, "inputSchema", getattr(t, "input_schema", {})),
                ))
            return tools
        except Exception as exc:
            self._log.exception("MCP %s list_tools error: %s", self.name, exc)
            return []

    async def call_tool(self, name: str, arguments: Optional[dict] = None) -> dict:
        """Call a tool on the MCP server.

        Returns a result dict with keys:
          - "content": list of content items
          - "is_error": bool
          - "summary": str (concatenated text content for display)
        """
        if not self._connected or self._session is None:
            return {"content": [], "is_error": True, "summary": f"MCP {self.name}: not connected"}

        try:
            result = await self._session.call_tool(name, arguments=arguments or {})

            content = list(getattr(result, "content", result if isinstance(result, list) else []))
            is_error = bool(getattr(result, "isError", False))

            # Build text summary
            summary_parts = []
            for c in content:
                if isinstance(c, dict):
                    if c.get("type") == "text":
                        summary_parts.append(c.get("text", ""))
                    elif c.get("type") == "resource":
                        summary_parts.append(str(c.get("resource", "")))
                elif hasattr(c, "type"):
                    if c.type == "text":
                        summary_parts.append(c.text if hasattr(c, "text") else str(c))
                else:
                    summary_parts.append(str(c))

            return {
                "content": content,
                "is_error": is_error,
                "summary": "\n".join(summary_parts),
            }
        except Exception as exc:
            self._log.exception("MCP %s call_tool(%s) error: %s", self.name, name, exc)
            return {
                "content": [{"type": "text", "text": str(exc)}],
                "is_error": True,
                "summary": f"Error: {exc}",
            }

    @property
    def info(self) -> dict:
        return {
            "name": self.name,
            "command": self.command,
            "args": self.args,
            "transport": self.transport,
            "url": self.url,
            "headers": bool(self.headers),
            "connected": self._connected,
        }
