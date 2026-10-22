"""UDP discovery responder — lets MCP servers announce themselves to the aggregator."""

import asyncio
import json
import logging
import socket
import struct
from typing import Any, Callable

logger = logging.getLogger(__name__)

MULTICAST_GROUP = "239.255.99.1"

# Ack mode: when a discovery request carries "ack", reply with 8 bytes —
# b"BCNA" + the FNV-1a 32-bit hash of the manifest, big-endian — and send the
# manifest itself only when Beacon asks for it 1:1 with {"type": "manifest"}.
# Full manifests answered to a broadcast all land on Beacon at once and
# overflow its socket buffer; an ack never can. Requests without "ack" (an
# older Beacon) still get the full manifest.
ACK_MAGIC = b"BCNA"


def fnv1a32(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


class _AnnounceProtocol(asyncio.DatagramProtocol):
    def __init__(self, manifest: dict[str, Any], on_discovery: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.manifest = json.dumps(manifest).encode()
        self.ack = ACK_MAGIC + struct.pack(">I", fnv1a32(self.manifest))
        self.transport: asyncio.DatagramTransport | None = None
        self.on_discovery = on_discovery

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            msg = json.loads(data)
            if msg.get("type") == "manifest":
                self.transport.sendto(self.manifest, addr)
            elif msg.get("type") == "discovery":
                logger.info("Discovery request from %s, %s", addr, "acking" if msg.get("ack") else "announcing")
                self.transport.sendto(self.ack if msg.get("ack") else self.manifest, addr)
                if self.on_discovery:
                    self.on_discovery({"mcp_url": msg.get("mcp_url")})
        except (json.JSONDecodeError, KeyError, AttributeError, UnicodeDecodeError):
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
              Each tool dict may include an optional ``"direct": True`` flag
              to have Beacon expose the tool as a first-class MCP tool alongside
              its meta-tools, instead of only through ``beacon_call``.
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

    # Join multicast group so we receive discovery packets on networks
    # where broadcast doesn't work (e.g. plain `docker network create`)
    sock = transport.get_extra_info("socket")
    group = socket.inet_aton(MULTICAST_GROUP)
    mreq = struct.pack("4sL", group, socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    logger.info("Discovery responder listening on UDP :%d (multicast %s) for %s", listen_port, MULTICAST_GROUP, name)
    return transport
