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

# Ack mode. A responder that sees "ack" in the discovery request answers with
# 8 bytes per manifest it serves — ACK_MAGIC + the FNV-1a 32-bit hash of that
# manifest, big-endian — instead of the manifest itself, and Beacon asks for a
# manifest 1:1, by hash ({"type":"manifest","rev":"<8 hex>"}), only when it has
# not seen that hash from that responder. A responder hosting several servers
# sends one ack each, which is why the hash, not the address, names a
# manifest. Both fields are strings so that a responder decoding the request
# into a string map still parses it. Every server answers a sweep
# at the same instant; with full manifests (up to ~32 KB each, sent twice —
# once per multicast and once per broadcast) that burst overflowed the
# discovery socket's receive buffer and the kernel dropped whole replies,
# so servers vanished from the registry for a cycle at random. A responder
# that predates ack mode ignores the extra field and still sends its full
# announce, which is still accepted below.
ACK_MAGIC = b"BCNA"
ACK_LEN = 8
MANIFEST_TIMEOUT = 1.0
MANIFEST_ATTEMPTS = 3


@dataclass
class DiscoveryResponse:
    name: str
    description: str
    tools: list[dict]
    ip: str
    port: int
    path: str = "/mcp"
    auth: dict | None = None


def parse_announce(data: bytes, ip: str) -> DiscoveryResponse | None:
    """A full announce (the legacy reply, and the reply to a manifest request), or None."""
    try:
        payload = json.loads(data)
        if payload.get("type") != "announce":
            return None
        return DiscoveryResponse(
            name=payload["name"],
            description=payload.get("description", ""),
            tools=payload.get("tools", []),
            ip=ip,
            port=payload.get("port", 9099),
            path=payload.get("path", "/mcp"),
            auth=payload.get("auth"),
        )
    except (json.JSONDecodeError, KeyError, AttributeError, UnicodeDecodeError) as e:
        logger.debug("Ignoring malformed announce from %s: %s", ip, e)
        return None


class DiscoveryProtocol(asyncio.DatagramProtocol):
    """Collects UDP responses from MCP servers: acks, and full announces from
    responders that predate ack mode."""

    def __init__(self) -> None:
        self.announces: list[DiscoveryResponse] = []
        # (ip, udp port, manifest hash)
        self.acks: set[tuple[str, int, int]] = set()
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) == ACK_LEN and data[:4] == ACK_MAGIC:
            self.acks.add((addr[0], addr[1], struct.unpack(">I", data[4:])[0]))
            return
        resp = parse_announce(data, addr[0])
        if resp and not any(r.name == resp.name for r in self.announces):
            self.announces.append(resp)

    def error_received(self, exc: Exception) -> None:
        logger.debug("Discovery protocol error: %s", exc)


class _ManifestProtocol(asyncio.DatagramProtocol):
    def __init__(self, ip: str) -> None:
        self.ip = ip
        self.result: asyncio.Future[DiscoveryResponse] = asyncio.get_running_loop().create_future()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        resp = parse_announce(data, addr[0])
        if resp and not self.result.done():
            self.result.set_result(resp)


async def fetch_manifest(ip: str, udp_port: int, rev: int) -> DiscoveryResponse | None:
    """Ask one responder for its manifest, on a socket of its own.

    One reply per socket is what keeps a large manifest from ever sharing a
    receive buffer with another server's."""
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: _ManifestProtocol(ip), local_addr=("0.0.0.0", 0)
    )
    msg = json.dumps({"type": "manifest", "rev": f"{rev:08x}"}).encode()
    try:
        for _ in range(MANIFEST_ATTEMPTS):
            transport.sendto(msg, (ip, udp_port))
            try:
                return await asyncio.wait_for(asyncio.shield(protocol.result), MANIFEST_TIMEOUT)
            except asyncio.TimeoutError:
                continue
        return None
    finally:
        transport.close()


class ManifestCache:
    """Manifests by (ip, udp port, hash) — content-addressed, so a stable
    server costs one 8-byte ack per sweep and nothing else, and a changed
    manifest is fetched because its hash is new."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, int, int], DiscoveryResponse] = {}

    async def resolve(self, acks: set[tuple[str, int, int]]) -> list[DiscoveryResponse]:
        async def one(key: tuple[str, int, int]) -> DiscoveryResponse | None:
            if key in self._cache:
                return self._cache[key]
            resp = await fetch_manifest(*key)
            if resp is None:
                logger.warning(
                    "No manifest %08x from %s:%d after %d attempts", key[2], key[0], key[1], MANIFEST_ATTEMPTS
                )
                return None
            self._cache[key] = resp
            return resp

        results = await asyncio.gather(*(one(k) for k in acks))
        # Forget what was not acked this sweep: a superseded hash, or a
        # recreated container that came back on a new IP.
        for key in set(self._cache) - acks:
            del self._cache[key]
        return [r for r in results if r is not None]


async def run_discovery(
    port: int = 9099, timeout: float = 2.0, mcp_url: str | None = None, manifests: ManifestCache | None = None
) -> list[DiscoveryResponse]:
    """Send UDP broadcast and collect responses from MCP servers."""
    request = {"type": "discovery", "ack": "1"}
    if mcp_url:
        request["mcp_url"] = mcp_url
    msg = json.dumps(request).encode()
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
    finally:
        transport.close()

    acked = await (manifests or ManifestCache()).resolve(protocol.acks)
    responses: list[DiscoveryResponse] = []
    for resp in [*acked, *protocol.announces]:
        if not any(r.name == resp.name for r in responses):
            logger.info("Discovered server: %s at %s:%d", resp.name, resp.ip, resp.port)
            responses.append(resp)
    return responses


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
