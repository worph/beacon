"""MCP proxy server that exposes meta-tools for indirect tool access."""

import contextlib
import json
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

META_TOOLS = [
    types.Tool(
        name="overview",
        description="List all available tools across discovered MCP servers with names and short descriptions.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="tool_doc",
        description="Get the full schema and description for a specific tool.",
        inputSchema={
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Namespaced tool name, e.g. 'mock-notes__create_note'",
                },
            },
            "required": ["tool_name"],
        },
    ),
    types.Tool(
        name="server_doc",
        description="Get the full schema and description for all tools on a specific server.",
        inputSchema={
            "type": "object",
            "properties": {
                "server_name": {
                    "type": "string",
                    "description": "Server name as shown in overview, e.g. 'mock-notes'",
                },
            },
            "required": ["server_name"],
        },
    ),
    types.Tool(
        name="call",
        description="Call a tool on a discovered MCP server.",
        inputSchema={
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Namespaced tool name, e.g. 'mock-notes__create_note'",
                },
                "arguments": {
                    "type": "object",
                    "description": "Arguments to pass to the tool",
                    "additionalProperties": True,
                },
            },
            "required": ["tool_name"],
        },
    ),
]


def _create_mcp_server(registry: Registry) -> StreamableHTTPSessionManager:
    """Build the MCP server and return its session manager."""
    server = Server(
        name="mcp-aggregator",
        version="0.1.0",
        instructions=registry.get_instructions(),
    )

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        server.instructions = registry.get_instructions()
        tools = list(META_TOOLS)
        for t in registry.get_direct_tools():
            tools.append(
                types.Tool(
                    name=t["name"],
                    description=t.get("description", ""),
                    inputSchema=t.get("inputSchema", {"type": "object", "properties": {}}),
                )
            )
        return tools

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        arguments = arguments or {}

        if name == "overview":
            overview = registry.get_overview_text()
            if not overview:
                overview = "No servers discovered yet."
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=overview)],
            )

        if name == "tool_doc":
            tool_name = arguments.get("tool_name", "")
            doc = registry.get_tool_doc(tool_name)
            if doc is None:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=f"Unknown tool: {tool_name}")],
                    isError=True,
                )
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(doc, indent=2))],
            )

        if name == "server_doc":
            server_name = arguments.get("server_name", "")
            doc = registry.get_server_doc(server_name)
            if doc is None:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=f"Unknown server: {server_name}")],
                    isError=True,
                )
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(doc, indent=2))],
            )

        if name == "call":
            tool_name = arguments.get("tool_name", "")
            tool_args = arguments.get("arguments", {})
            resolved = registry.resolve_tool(tool_name)
            if resolved is None:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=f"Unknown tool: {tool_name}")],
                    isError=True,
                )
            srv, original_name = resolved
            return await call_remote_tool(srv.ip, srv.port, original_name, tool_args, srv.path, srv.auth)

        # Hybrid: direct tool call
        resolved = registry.resolve_tool(name)
        if resolved is None:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Unknown tool: {name}")],
                isError=True,
            )
        srv, tool_name = resolved
        return await call_remote_tool(srv.ip, srv.port, tool_name, arguments, srv.path, srv.auth)

    session_manager = StreamableHTTPSessionManager(app=server, stateless=True)
    return session_manager


def create_mcp_session_manager(registry: Registry) -> StreamableHTTPSessionManager:
    """Create just the session manager (for mounting into another app)."""
    return _create_mcp_server(registry)


def create_mcp_app(registry: Registry) -> Starlette:
    """Create a standalone Starlette ASGI app serving the MCP endpoint."""
    session_manager = _create_mcp_server(registry)

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
