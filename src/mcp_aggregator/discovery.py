"""UDP broadcast discovery for MCP servers on the local network."""

import asyncio
import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DISCOVERY_MSG = json.dumps({"type": "discovery"}).encode()


@dataclass
class DiscoveryResponse:
    name: str
    description: str
    tools: list[dict]
    ip: str
    port: int


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
            )
            # Deduplicate by name
            if not any(r.name == resp.name for r in self.responses):
                logger.info("Discovered server: %s at %s:%d", resp.name, resp.ip, resp.port)
                self.responses.append(resp)
        except (json.JSONDecodeError, KeyError) as e:
            logger.debug("Ignoring malformed discovery response from %s: %s", addr, e)

    def error_received(self, exc: Exception) -> None:
        logger.debug("Discovery protocol error: %s", exc)


async def run_discovery(port: int = 9099, timeout: float = 2.0) -> list[DiscoveryResponse]:
    """Send UDP broadcast and collect responses from MCP servers."""
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        DiscoveryProtocol,
        local_addr=("0.0.0.0", 0),
        allow_broadcast=True,
    )
    try:
        transport.sendto(DISCOVERY_MSG, ("255.255.255.255", port))
        logger.info("Sent discovery broadcast on port %d, waiting %.1fs...", port, timeout)
        await asyncio.sleep(timeout)
        return protocol.responses
    finally:
        transport.close()
