"""MCP Manager: manages multiple MCP server connections.

Uses Claude Code-compatible config format: `{"mcpServers": {"name": {config}}}`.
Config file at `~/.minicpm/mcp.json`.

Handles:
  - stdio transport (command + args)
  - http/sse transport (url + headers)
  - ${VAR} env variable expansion in config values
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, Optional

from ..log_setup import get_logger
from .mcp_client import MCPClient, MCPTool


DEFAULT_MCP_CONFIG = Path.home() / ".minicpm" / "mcp.json"

_ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def _expand_env(value: str) -> str:
    """Expand ${VAR} and ${VAR:-default} in a string."""
    def _replace(m):
        name = m.group(1)
        default = m.group(2)
        val = os.environ.get(name)
        if val is not None:
            return val
        if default is not None:
            return default
        return m.group(0)
    return _ENV_VAR_PATTERN.sub(_replace, value)


def _expand_dict(d: dict) -> dict:
    """Recursively expand env vars in all string values of a dict."""
    out = {}
    for k, v in d.items():
        if isinstance(v, str):
            out[k] = _expand_env(v)
        elif isinstance(v, dict):
            out[k] = _expand_dict(v)
        elif isinstance(v, list):
            out[k] = [_expand_env(x) if isinstance(x, str) else x for x in v]
        else:
            out[k] = v
    return out


class MCPManager:
    """Manages multiple MCP server connections."""

    BuiltinTool = dict  # {"name", "description", "input_schema", "handler": callable}

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self._config_path = config_path or DEFAULT_MCP_CONFIG
        self._clients: dict[str, MCPClient] = {}
        self._builtin_tools: dict[str, BuiltinTool] = {}
        self._log = get_logger()

    def register_builtin(self, tool: BuiltinTool) -> None:
        """Register a built-in tool that runs in-process (no external MCP server).

        The tool dict must include ``name``, ``description``, ``input_schema``,
        and ``handler`` (an async callable accepting ``arguments: dict`` and
        returning a result dict with ``content`` / ``is_error``).

        Built-in tools are exposed under the virtual ``"builtin"`` server name.
        """
        self._builtin_tools[tool["name"]] = tool

    # ── Config ──────────────────────────────────────────────────────

    def load_config_from_dict(self, servers: dict) -> None:
        """Load from a dict of `{"servername": {type, command?, args?, env?, url?, headers?}}`.

        Compatible with Claude Code / opencode .mcp.json format.
        """
        for name, cfg in servers.items():
            if not isinstance(cfg, dict):
                continue
            cfg = _expand_dict(cfg)
            transport = cfg.get("type", "stdio")
            if name in self._clients:
                continue
            client = MCPClient(
                name=name,
                command=cfg.get("command", ""),
                args=cfg.get("args", []),
                env=cfg.get("env"),
                transport=transport,
                url=cfg.get("url"),
                headers=cfg.get("headers"),
            )
            self._clients[name] = client

    async def load_config(self) -> dict:
        """Load from `~/.minicpm/mcp.json`. Returns parsed config dict."""
        try:
            path = self._config_path.expanduser().resolve()
            if not path.is_file():
                return {}
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
            servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
            if isinstance(servers, list):
                return {}
            self.load_config_from_dict(servers)
            return servers
        except Exception as exc:
            self._log.warning("Failed to load MCP config: %s", exc)
            return {}

    async def save_config(self, servers: dict) -> bool:
        """Save to `~/.minicpm/mcp.json` in `{"mcpServers": {...}}` format."""
        try:
            path = self._config_path.expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"mcpServers": servers}
            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return True
        except Exception as exc:
            self._log.warning("Failed to save MCP config: %s", exc)
            return False

    # ── Lifecycle ──────────────────────────────────────────────────

    async def connect_all(self) -> dict[str, bool]:
        results = {}
        for name, client in list(self._clients.items()):
            ok = await client.connect()
            results[name] = ok
            if not ok:
                self._log.warning("MCP %s failed to connect", name)
        return results

    async def disconnect_all(self) -> None:
        for name, client in list(self._clients.items()):
            try:
                await client.disconnect()
            except Exception as exc:
                self._log.debug("MCP %s disconnect error: %s", name, exc)

    async def connect_server(self, name: str) -> bool:
        client = self._clients.get(name)
        if client is None:
            return False
        return await client.connect()

    async def disconnect_server(self, name: str) -> bool:
        client = self._clients.get(name)
        if client is None:
            return False
        await client.disconnect()
        return True

    def add_server(self, name: str, command: str = "", args: Optional[list[str]] = None,
                   transport: str = "stdio", url: Optional[str] = None,
                   env: Optional[dict[str, str]] = None,
                   headers: Optional[dict[str, str]] = None) -> MCPClient:
        client = MCPClient(
            name=name, command=command, args=args or [],
            env=env, transport=transport, url=url, headers=headers,
        )
        self._clients[name] = client
        return client

    async def remove_server(self, name: str) -> bool:
        client = self._clients.pop(name, None)
        if client is None:
            return False
        try:
            await client.disconnect()
        except Exception:
            pass
        return True

    # ── Tools ──────────────────────────────────────────────────────

    async def list_all_tools(self) -> list[dict]:
        all_tools: list[dict] = []
        for name, client in self._clients.items():
            if not client._connected:
                continue
            try:
                tools = await client.list_tools()
                for tool in tools:
                    all_tools.append({
                        "server_name": name,
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": tool.input_schema,
                    })
            except Exception as exc:
                self._log.debug("MCP %s list_tools error: %s", name, exc)
        for tool in self._builtin_tools.values():
            all_tools.append({
                "server_name": "builtin",
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool["input_schema"],
            })
        return all_tools

    async def call_tool(self, server_name: str, tool_name: str,
                        arguments: Optional[dict] = None) -> dict:
        # Built-in tool path
        if server_name == "builtin":
            tool = self._builtin_tools.get(tool_name)
            if tool is None:
                return {
                    "content": [{"type": "text", "text": f"Unknown builtin tool: {tool_name}"}],
                    "is_error": True,
                }
            handler = tool.get("handler")
            if handler is None:
                return {
                    "content": [{"type": "text", "text": f"Builtin tool {tool_name} has no handler"}],
                    "is_error": True,
                }
            try:
                return await handler(arguments or {})
            except Exception as exc:
                self._log.warning("builtin tool %s failed: %s", tool_name, exc)
                return {
                    "content": [{"type": "text", "text": f"{tool_name} failed: {exc}"}],
                    "is_error": True,
                }

        # External MCP server path
        client = self._clients.get(server_name)
        if client is None:
            # Fallback: if tool_name matches a builtin, use it regardless
            # of server_name (models sometimes confuse skill names with
            # MCP server names, e.g. "screen-observe" vs "builtin").
            builtin = self._builtin_tools.get(tool_name)
            if builtin is not None:
                handler = builtin.get("handler")
                if handler is not None:
                    try:
                        return await handler(arguments or {})
                    except Exception as exc:
                        self._log.warning("builtin tool %s failed: %s", tool_name, exc)
                        return {
                            "content": [{"type": "text", "text": f"{tool_name} failed: {exc}"}],
                            "is_error": True,
                        }
            return {
                "content": [{"type": "text", "text": f"Unknown MCP server: {server_name}"}],
                "is_error": True,
                "summary": f"Unknown MCP server: {server_name}",
            }
        if not client._connected:
            ok = await client.connect()
            if not ok:
                return {
                    "content": [{"type": "text", "text": f"MCP server {server_name} not connected"}],
                    "is_error": True,
                    "summary": f"MCP server {server_name} not connected",
                }
        return await client.call_tool(tool_name, arguments=arguments)

    def snapshot_config(self) -> dict[str, dict]:
        servers: dict[str, dict] = {}
        for name, client in self._clients.items():
            entry: dict = {"type": client.transport}
            if client.command:
                entry["command"] = client.command
            if client.args:
                entry["args"] = client.args
            if client.url:
                entry["url"] = client.url
            if client.env:
                entry["env"] = client.env
            if client.headers:
                entry["headers"] = client.headers
            servers[name] = entry
        return servers

    async def status(self) -> list[dict]:
        results = []
        for name, client in self._clients.items():
            info = client.info
            if client._connected:
                try:
                    tools = await client.list_tools()
                    info["tool_count"] = len(tools)
                except Exception:
                    info["tool_count"] = 0
            else:
                info["tool_count"] = 0
            results.append(info)
        return results
