"""UDP multicast + broadcast discovery for MCP servers on the local network."""

import asyncio
import json
import logging
import socket
import struct
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MULTICAST_GROUP = "239.255.99.1"
DEFAULT_DISCOVERY_MSG = json.dumps({"type": "discovery"}).encode()


@dataclass
class DiscoveryResponse:
    name: str
    description: str
    tools: list[dict]
    ip: str
    port: int
    path: str = "/mcp"
    auth: dict | None = None


class DiscoveryProtocol(asyncio.DatagramProtocol):
    """Collects UDP responses from MCP servers."""

    def __init__(self) -> None:
        self.responses: list[DiscoveryResponse] = []
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            payload = json.loads(data)
            if payload.get("type") != "announce":
                return
            resp = DiscoveryResponse(
                name=payload["name"],
                description=payload.get("description", ""),
                tools=payload.get("tools", []),
                ip=addr[0],
                port=payload.get("port", 9099),
                path=payload.get("path", "/mcp"),
                auth=payload.get("auth"),
            )
            # Deduplicate by name
            if not any(r.name == resp.name for r in self.responses):
                logger.info("Discovered server: %s at %s:%d", resp.name, resp.ip, resp.port)
                self.responses.append(resp)
        except (json.JSONDecodeError, KeyError) as e:
            logger.debug("Ignoring malformed discovery response from %s: %s", addr, e)

    def error_received(self, exc: Exception) -> None:
        logger.debug("Discovery protocol error: %s", exc)


async def run_discovery(port: int = 9099, timeout: float = 2.0, mcp_url: str | None = None) -> list[DiscoveryResponse]:
    """Send UDP broadcast and collect responses from MCP servers."""
    if mcp_url:
        msg = json.dumps({"type": "discovery", "mcp_url": mcp_url}).encode()
    else:
        msg = DEFAULT_DISCOVERY_MSG
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        DiscoveryProtocol,
        local_addr=("0.0.0.0", 0),
        allow_broadcast=True,
    )
    try:
        # Set multicast TTL to 1 (link-local only)
        sock = transport.get_extra_info("socket")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)

        # Send to both multicast group and broadcast for maximum compatibility
        transport.sendto(msg, (MULTICAST_GROUP, port))
        transport.sendto(msg, ("255.255.255.255", port))
        logger.info("Sent discovery multicast+broadcast on port %d, waiting %.1fs...", port, timeout)
        await asyncio.sleep(timeout)
        return protocol.responses
    finally:
        transport.close()
