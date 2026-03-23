"""MCP client for calling tools on remote MCP servers."""

import logging
import os
from typing import Any

import httpx
import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger(__name__)

MCP_CLIENT_TIMEOUT = float(os.environ.get("MCP_CLIENT_TIMEOUT", "300"))


def _build_headers(auth: dict | None) -> dict[str, str]:
    """Build HTTP headers from an auth descriptor."""
    if not auth:
        return {}
    if auth.get("type") == "bearer":
        return {"Authorization": f"Bearer {auth['token']}"}
    logger.warning("Unknown auth type: %s", auth.get("type"))
    return {}


async def call_remote_tool(
    server_ip: str,
    server_port: int,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    path: str = "/mcp",
    auth: dict | None = None,
) -> types.CallToolResult:
    """Call a tool on a remote MCP server via streamable HTTP."""
    url = f"http://{server_ip}:{server_port}{path}"
    logger.info("Calling %s on %s", tool_name, url)
    headers = _build_headers(auth)
    try:
        timeout = httpx.Timeout(MCP_CLIENT_TIMEOUT, connect=10.0)
        http_client = httpx.AsyncClient(headers=headers, timeout=timeout)
        async with streamable_http_client(url, http_client=http_client) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                return result
    except BaseException as e:
        logger.error("Error calling %s on %s: %s", tool_name, url, e, exc_info=True)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Error calling remote tool: {e}")],
            isError=True,
        )
