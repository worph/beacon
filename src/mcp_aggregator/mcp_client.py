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

# Sentinel for "caller did not override the timeout" — distinct from an explicit
# None, which means "no timeout at all" (e.g. __beacon_timeout=0).
_USE_DEFAULT: Any = object()


def build_auth_headers(auth: dict | None) -> dict[str, str]:
    """Build HTTP headers from an auth descriptor used by discovered servers."""
    if not auth:
        return {}
    auth_type = auth.get("type")
    if auth_type == "bearer":
        return {"Authorization": f"Bearer {auth['token']}"}
    # Fail loud: silently dropping auth leads to mystery 401s downstream.
    raise ValueError(f"unsupported auth type {auth_type!r}")


def format_exc(e: BaseException) -> str:
    """Flatten ExceptionGroup sub-exceptions into a readable string.

    anyio task groups (used inside the MCP streamable_http client) wrap failures
    in BaseExceptionGroup whose str() is the unhelpful "unhandled errors in a
    TaskGroup (N sub-exception)". Walk the group so the caller sees the real cause.
    """
    if isinstance(e, BaseExceptionGroup):
        inner = [format_exc(sub) for sub in e.exceptions]
        return "; ".join(inner) if inner else repr(e)
    msg = str(e) or repr(e)
    return f"{type(e).__name__}: {msg}"


def leaf_exceptions(e: BaseException) -> list[BaseException]:
    """Flatten a (possibly nested) ExceptionGroup into its leaf exceptions."""
    if isinstance(e, BaseExceptionGroup):
        out: list[BaseException] = []
        for sub in e.exceptions:
            out.extend(leaf_exceptions(sub))
        return out
    return [e]


def is_timeout_error(leaves: list[BaseException]) -> bool:
    """True if any leaf is a read/connect timeout (httpx) or asyncio/builtin TimeoutError."""
    return any(isinstance(x, (httpx.TimeoutException, TimeoutError)) for x in leaves)


async def call_remote_tool(
    url: str,
    headers: dict[str, str],
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    timeout: Any = _USE_DEFAULT,
    display_name: str | None = None,
) -> types.CallToolResult:
    """Call a tool on a remote MCP server via streamable HTTP.

    `tool_name` is the bare name sent to the remote server; `display_name` is the
    namespaced name the caller used (e.g. 'blackhole__hang') and is used only in
    log/error text so a failure names the tool the caller actually invoked.

    timeout semantics:
      - _USE_DEFAULT  → use MCP_CLIENT_TIMEOUT (the global default)
      - None          → no timeout (read/write/pool unbounded); only `connect` is bounded
      - float (> 0)   → that many seconds
    """
    label = display_name or tool_name
    effective = MCP_CLIENT_TIMEOUT if timeout is _USE_DEFAULT else timeout
    logger.info("Calling %s on %s (timeout=%s)", label, url, effective)
    try:
        # Keep a bounded connect timeout even when the read timeout is lifted, so a
        # dead host fails fast instead of hanging on the TCP handshake.
        connect = 10.0 if effective is None or effective >= 10.0 else effective
        http_timeout = httpx.Timeout(effective, connect=connect)
        async with httpx.AsyncClient(headers=headers, timeout=http_timeout) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    return await session.call_tool(tool_name, arguments)
    except asyncio.CancelledError:
        # Genuine external cancellation (caller gone / shutdown) — never swallow it.
        raise
    except (Exception, BaseExceptionGroup) as e:
        # A read timeout cancels the streamable-HTTP stream tasks, so the failure
        # surfaces as a BaseExceptionGroup (a BaseException, NOT an Exception) wrapping
        # the real cause + sibling CancelledErrors. The old `except Exception` missed it,
        # leaking the raw "unhandled errors in a TaskGroup" string. Catch both, flatten,
        # and only re-raise if it is *purely* external cancellation (no real error).
        leaves = leaf_exceptions(e)
        if leaves and all(isinstance(x, asyncio.CancelledError) for x in leaves):
            raise
        logger.error("Error calling %s on %s: %s", label, url, format_exc(e), exc_info=True)
        if is_timeout_error(leaves):
            cap = "no timeout" if effective is None else f"{effective}s"
            text = f"Timed out ({cap}) calling remote tool {label} at {url}"
        else:
            text = f"Error calling remote tool {label} at {url}: {format_exc(e)}"
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            isError=True,
        )


async def fetch_remote_tools(
    url: str,
    headers: dict[str, str],
    connect_timeout: float = 10.0,
    read_timeout: float = 30.0,
) -> tuple[str, list[dict]]:
    """Connect to a remote MCP server and return (server instructions, tool list)."""
    timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
    async with httpx.AsyncClient(headers=headers, timeout=timeout) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                init_result = await session.initialize()
                tools_result = await session.list_tools()
                tools = [
                    {
                        "name": t.name,
                        "description": t.description or "",
                        "inputSchema": t.inputSchema or {"type": "object", "properties": {}},
                    }
                    for t in tools_result.tools
                ]
                return (init_result.instructions or "", tools)
