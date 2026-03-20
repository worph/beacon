"""MCP client for calling tools on remote MCP servers."""

import logging
from typing import Any

import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger(__name__)


async def call_remote_tool(
    server_ip: str,
    server_port: int,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> types.CallToolResult:
    """Call a tool on a remote MCP server via streamable HTTP."""
    url = f"http://{server_ip}:{server_port}/mcp"
    logger.info("Calling %s on %s", tool_name, url)
    try:
        async with streamable_http_client(url) as (read_stream, write_stream, _):
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
