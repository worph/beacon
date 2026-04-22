"""MCP client for calling tools on remote MCP servers."""

import asyncio
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
    auth_type = auth.get("type")
    if auth_type == "bearer":
        return {"Authorization": f"Bearer {auth['token']}"}
    # Fail loud: silently dropping auth leads to mystery 401s downstream.
    raise ValueError(f"unsupported auth type {auth_type!r}")


def _format_exc(e: BaseException) -> str:
    """Flatten ExceptionGroup sub-exceptions into a readable string.

    anyio task groups (used inside the MCP streamable_http client) wrap failures
    in BaseExceptionGroup whose str() is the unhelpful "unhandled errors in a
    TaskGroup (N sub-exception)". Walk the group so the caller sees the real cause.
    """
    if isinstance(e, BaseExceptionGroup):
        inner = [_format_exc(sub) for sub in e.exceptions]
        return "; ".join(inner) if inner else repr(e)
    msg = str(e) or repr(e)
    return f"{type(e).__name__}: {msg}"


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
    try:
        headers = _build_headers(auth)
        timeout = httpx.Timeout(MCP_CLIENT_TIMEOUT, connect=10.0)
        async with httpx.AsyncClient(headers=headers, timeout=timeout) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    return await session.call_tool(tool_name, arguments)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error("Error calling %s on %s: %s", tool_name, url, e, exc_info=True)
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=f"Error calling remote tool {tool_name} at {url}: {_format_exc(e)}",
                )
            ],
            isError=True,
        )
