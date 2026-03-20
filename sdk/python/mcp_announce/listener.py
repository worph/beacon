"""UDP discovery responder — lets MCP servers announce themselves to the aggregator."""

import asyncio
import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


class _AnnounceProtocol(asyncio.DatagramProtocol):
    def __init__(self, manifest: dict[str, Any], on_discovery: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.manifest = json.dumps(manifest).encode()
        self.transport: asyncio.DatagramTransport | None = None
        self.on_discovery = on_discovery

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            msg = json.loads(data)
            if msg.get("type") == "discovery":
                logger.info("Discovery request from %s, announcing", addr)
                self.transport.sendto(self.manifest, addr)
                if self.on_discovery:
                    self.on_discovery({"mcp_url": msg.get("mcp_url")})
        except (json.JSONDecodeError, KeyError):
            pass

    def error_received(self, exc: Exception) -> None:
        logger.debug("Announce protocol error: %s", exc)


async def create_discovery_responder(
    name: str,
    description: str,
    tools: list[dict[str, Any]],
    port: int = 9099,
    path: str | None = None,
    listen_port: int = 9099,
    auth: dict[str, str] | None = None,
    on_discovery: Callable[[dict[str, Any]], None] | None = None,
) -> asyncio.DatagramTransport:
    """Start a UDP listener that responds to discovery broadcasts.

    Args:
        name: Server name for identification.
        description: Human-readable description.
        tools: List of tool definitions (name, description, inputSchema).
        port: The HTTP port where the MCP server is listening.
        path: HTTP path for the MCP endpoint (default: /mcp). Set if your
              server uses a non-standard path like /api/mcp.
        listen_port: UDP port to listen on for discovery broadcasts.
        auth: Optional auth descriptor, e.g. {"type": "bearer", "token": "secret"}.
              Passed to the aggregator so it can authenticate when calling tools.
        on_discovery: Optional callback invoked when a discovery message is received.
              Called with {"mcp_url": ...} if the broadcast includes it.

    Returns:
        The transport (call .close() to stop).
    """
    manifest: dict[str, Any] = {
        "type": "announce",
        "name": name,
        "description": description,
        "tools": tools,
        "port": port,
    }
    if path:
        manifest["path"] = path
    if auth:
        manifest["auth"] = auth
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _AnnounceProtocol(manifest, on_discovery=on_discovery),
        local_addr=("0.0.0.0", listen_port),
        allow_broadcast=True,
    )
    logger.info("Discovery responder listening on UDP :%d for %s", listen_port, name)
    return transport
