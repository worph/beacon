"""UDP discovery responder — lets MCP servers announce themselves to the aggregator."""

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class _AnnounceProtocol(asyncio.DatagramProtocol):
    def __init__(self, manifest: dict[str, Any]) -> None:
        self.manifest = json.dumps(manifest).encode()
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            msg = json.loads(data)
            if msg.get("type") == "discovery":
                logger.info("Discovery request from %s, announcing", addr)
                self.transport.sendto(self.manifest, addr)
        except (json.JSONDecodeError, KeyError):
            pass

    def error_received(self, exc: Exception) -> None:
        logger.debug("Announce protocol error: %s", exc)


async def create_discovery_responder(
    name: str,
    description: str,
    tools: list[dict[str, Any]],
    port: int = 9099,
    listen_port: int = 9099,
) -> asyncio.DatagramTransport:
    """Start a UDP listener that responds to discovery broadcasts.

    Args:
        name: Server name for identification.
        description: Human-readable description.
        tools: List of tool definitions (name, description, inputSchema).
        port: The HTTP port where the MCP server is listening.
        listen_port: UDP port to listen on for discovery broadcasts.

    Returns:
        The transport (call .close() to stop).
    """
    manifest = {
        "type": "announce",
        "name": name,
        "description": description,
        "tools": tools,
        "port": port,
    }
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _AnnounceProtocol(manifest),
        local_addr=("0.0.0.0", listen_port),
        allow_broadcast=True,
    )
    logger.info("Discovery responder listening on UDP :%d for %s", listen_port, name)
    return transport
