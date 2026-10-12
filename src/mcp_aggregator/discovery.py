"""UDP multicast + broadcast discovery for MCP servers on the local network."""

import asyncio
import json
import logging
import socket
import struct
from dataclasses import dataclass, field

from mcp_aggregator.mcp_client import build_auth_headers, fetch_remote_tools, format_exc

logger = logging.getLogger(__name__)

# Upper bound for one schema fetch, so an unresponsive server can't stall the
# discovery loop (the streamable-HTTP client can hang on a malformed response).
SCHEMA_FETCH_TIMEOUT = 15.0

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


class SchemaFetcher:
    """Completes discovered tool lists with full definitions fetched over HTTP.

    A UDP announce must fit in one 64 KB datagram, so large servers announce
    only {name, description} — and a server whose catalog isn't loaded yet
    announces none. Without inputSchema a caller has to guess every argument,
    so for those servers we run `tools/list` against the announced endpoint.
    Results are cached by endpoint + announced tool names, so a stable server
    is fetched once rather than on every discovery cycle.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, tuple[str, ...]], list[dict]] = {}

    @staticmethod
    def _needs_fetch(resp: DiscoveryResponse) -> bool:
        return not resp.tools or any("inputSchema" not in t for t in resp.tools)

    async def enrich(self, responses: list[DiscoveryResponse]) -> None:
        await asyncio.gather(*(self._enrich_one(r) for r in responses if self._needs_fetch(r)))

    async def _enrich_one(self, resp: DiscoveryResponse) -> None:
        url = f"http://{resp.ip}:{resp.port}{resp.path}"
        names = tuple(sorted(t.get("name", "") for t in resp.tools))
        # An empty announce is never cached: it usually means the server's
        # catalog isn't ready yet, and the next cycle should look again.
        key = (url, names)
        if names and key in self._cache:
            resp.tools = self._cache[key]
            return
        try:
            info = await asyncio.wait_for(
                fetch_remote_tools(url, build_auth_headers(resp.auth)), SCHEMA_FETCH_TIMEOUT
            )
        except Exception as e:  # noqa: BLE001 — keep the announced list on any failure
            logger.warning("Could not fetch full tool list for %s at %s: %s", resp.name, url, format_exc(e))
            return
        if not info.tools:
            return
        resp.tools = info.tools
        if names:
            self._cache[key] = info.tools
        logger.info("Fetched %d tool definition(s) for %s over HTTP", len(info.tools), resp.name)
