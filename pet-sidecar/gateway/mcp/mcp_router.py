"""FastAPI routes for MCP server management.

Uses Claude Code-compatible object-map format:
  {"mcpServers": {"name": {type, command?, args?, url?, env?, headers?}}}
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .mcp_manager import MCPManager


class McpServerConfig(BaseModel):
    """MCP server configuration. Directly maps to Claude Code mcp.json entries."""
    name: str = Field(..., min_length=1)
    type: str = Field(default="stdio", pattern="^(stdio|http|sse)$")
    command: Optional[str] = Field(None)
    args: list[str] = Field(default_factory=list)
    url: Optional[str] = Field(None)
    env: Optional[dict[str, str]] = Field(None)
    headers: Optional[dict[str, str]] = Field(None)


class McpToolCallRequest(BaseModel):
    server_name: str
    tool_name: str
    arguments: dict = Field(default_factory=dict)


def create_mcp_router(mcp_manager: MCPManager) -> APIRouter:
    router = APIRouter(tags=["mcp"])

    @router.get("/api/mcp/servers")
    async def list_servers():
        servers = await mcp_manager.status()
        all_tools = await mcp_manager.list_all_tools()
        return {"servers": servers, "tools": all_tools, "count": len(servers)}

    @router.post("/api/mcp/servers")
    async def add_server(cfg: McpServerConfig):
        existing = await mcp_manager.status()
        if any(s["name"] == cfg.name for s in existing):
            raise HTTPException(status_code=409, detail=f"Server '{cfg.name}' already exists")

        mcp_manager.add_server(
            name=cfg.name,
            command=cfg.command or "",
            args=cfg.args,
            transport=cfg.type,
            url=cfg.url,
            env=cfg.env,
            headers=cfg.headers,
        )
        ok = False
        try:
            ok = await mcp_manager.connect_server(cfg.name)
        except Exception as exc:
            mcp_manager._log.warning("MCP %s connect failed: %s", cfg.name, exc)

        await mcp_manager.save_config(mcp_manager.snapshot_config())

        return {"ok": ok, "name": cfg.name, "connected": ok}

    @router.delete("/api/mcp/servers/{name}")
    async def remove_server(name: str):
        found = await mcp_manager.remove_server(name)
        if not found:
            raise HTTPException(status_code=404, detail=f"Server '{name}' not found")

        await mcp_manager.save_config(mcp_manager.snapshot_config())

        return {"ok": True, "name": name}

    @router.post("/api/mcp/execute")
    async def execute_tool(req: McpToolCallRequest):
        result = await mcp_manager.call_tool(
            server_name=req.server_name,
            tool_name=req.tool_name,
            arguments=req.arguments,
        )
        return result

    @router.post("/api/mcp/reconnect")
    async def reconnect_all():
        results = await mcp_manager.connect_all()
        return {"ok": True, "results": results}

    return router
