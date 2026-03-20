"""MCP proxy server that exposes namespaced tools from discovered servers."""

import contextlib
import logging
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount

from mcp_aggregator.mcp_client import call_remote_tool
from mcp_aggregator.registry import Registry

logger = logging.getLogger(__name__)


def create_mcp_app(registry: Registry) -> Starlette:
    """Create a Starlette ASGI app serving the aggregated MCP endpoint."""
    server = Server(name="mcp-aggregator", version="0.1.0")

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        namespaced = registry.get_all_namespaced_tools()
        return [
            types.Tool(
                name=t["name"],
                description=t.get("description", ""),
                inputSchema=t.get("inputSchema", {"type": "object", "properties": {}}),
            )
            for t in namespaced
        ]

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        resolved = registry.resolve_tool(name)
        if resolved is None:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Unknown tool: {name}")],
                isError=True,
            )
        srv, tool_name = resolved
        return await call_remote_tool(srv.ip, srv.port, tool_name, arguments, srv.path, srv.auth)

    session_manager = StreamableHTTPSessionManager(app=server, stateless=True)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with session_manager.run():
            yield

    return Starlette(
        routes=[
            Mount("/mcp", app=session_manager.handle_request),
        ],
        lifespan=lifespan,
    )
