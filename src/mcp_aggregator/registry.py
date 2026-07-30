"""Registry of discovered and externally-configured MCP servers."""

import logging
import time
from dataclasses import dataclass, field

from mcp_aggregator.discovery import DiscoveryResponse

logger = logging.getLogger(__name__)

NAMESPACE_SEP = "__"


@dataclass
class RegisteredServer:
    name: str
    description: str
    tools: list[dict]
    # Local discovered servers use ip/port/path/auth. External servers use url/headers.
    ip: str = ""
    port: int = 0
    path: str = "/mcp"
    auth: dict | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    origin: str = "discovery"  # "discovery" | "external"
    last_seen: float = field(default_factory=time.time)
    error: str | None = None
    # Federation: this server lives behind another Beacon rather than being
    # reachable directly. `federated_via` is the external config whose URL we
    # talk to; `remote_name` is what the server is called over there, which is
    # what the remote `call` meta-tool expects. `origin_chain` records the
    # Beacons a server has already travelled through, so re-export can't loop.
    federated_via: str | None = None
    remote_name: str | None = None
    origin_chain: list[str] = field(default_factory=list)
    # True when the server needs an interactive OAuth authorization before it
    # can be reached — distinct from a generic connection error.
    auth_required: bool = False

    def endpoint_url(self) -> str:
        if self.url:
            return self.url
        return f"http://{self.ip}:{self.port}{self.path}"


class Registry:
    """Stores MCP servers (discovered + external) and provides namespaced tool lookups."""

    def __init__(self, annotations=None, beacon_id: str = "") -> None:
        self._discovered: dict[str, RegisteredServer] = {}
        self._external: dict[str, RegisteredServer] = {}
        # One external *config* can contribute several registry entries (a
        # federated Beacon contributes one per remote server), so ownership is
        # tracked separately: registry name -> external config name.
        self._external_owner: dict[str, str] = {}
        # Optional AnnotationStore: provides per-server description overrides.
        self._annotations = annotations
        # Stable identity of this Beacon, stamped into exported registries so a
        # federation cycle is detectable.
        self.beacon_id = beacon_id

    def describe(self, server: RegisteredServer) -> str:
        """Effective description for a server: user override if set, else discovered."""
        if self._annotations is not None:
            override = self._annotations.get(server.name)
            if override:
                return override
        return server.description

    @property
    def servers(self) -> dict[str, RegisteredServer]:
        """All servers, discovered merged with external. External wins on name collision."""
        merged = dict(self._discovered)
        for name, srv in self._external.items():
            if name in merged:
                logger.warning("External server %r shadows a discovered server with the same name", name)
            merged[name] = srv
        return merged

    def update_from_discovery(self, responses: list[DiscoveryResponse]) -> None:
        """Full replace of the discovered set. Leaves external servers untouched."""
        now = time.time()
        new_servers: dict[str, RegisteredServer] = {}
        for resp in responses:
            new_servers[resp.name] = RegisteredServer(
                name=resp.name,
                description=resp.description,
                ip=resp.ip,
                port=resp.port,
                tools=resp.tools,
                path=resp.path,
                auth=resp.auth,
                origin="discovery",
                last_seen=now,
            )
        added = set(new_servers) - set(self._discovered)
        removed = set(self._discovered) - set(new_servers)
        if added:
            logger.info("New servers: %s", added)
        if removed:
            logger.info("Removed servers: %s", removed)
        self._discovered = new_servers

    def replace_external_group(self, config_name: str, servers: dict[str, RegisteredServer]) -> None:
        """Replace every registry entry owned by an external config.

        A plain external server contributes one entry keyed by its own name; a
        federated Beacon contributes one per remote server. Either way the whole
        group is swapped atomically so a shrinking remote does not leave ghosts.
        """
        for name, owner in list(self._external_owner.items()):
            if owner == config_name and name not in servers:
                self._external.pop(name, None)
                del self._external_owner[name]
        for name, server in servers.items():
            self._external[name] = server
            self._external_owner[name] = config_name

    def remove_external_group(self, config_name: str) -> bool:
        """Drop every registry entry owned by an external config."""
        names = [n for n, owner in self._external_owner.items() if owner == config_name]
        for name in names:
            self._external.pop(name, None)
            del self._external_owner[name]
        return bool(names)

    def external_group(self, config_name: str) -> list[RegisteredServer]:
        return [
            self._external[n]
            for n, owner in self._external_owner.items()
            if owner == config_name and n in self._external
        ]

    def list_external(self) -> list[RegisteredServer]:
        return list(self._external.values())

    def get_all_namespaced_tools(self) -> list[dict]:
        """Return all tools with namespace-prefixed names."""
        tools = []
        for server in self.servers.values():
            for tool in server.tools:
                namespaced = tool.copy()
                namespaced["name"] = f"{server.name}{NAMESPACE_SEP}{tool['name']}"
                tools.append(namespaced)
        return tools

    def get_instructions(self) -> str:
        """Build server instructions with a one-liner per server."""
        lines = ["Beacon MCP aggregator. Call server_doc with a server name to get full tool schemas for that server.", ""]
        note = self._annotations.get_instructions_note() if self._annotations is not None else ""
        if note:
            lines.append(note)
            lines.append("")
        lines.append("Available servers:")
        for server in self.servers.values():
            lines.append(f"- {server.name} — {self.describe(server)}")
        return "\n".join(lines)

    def get_overview_text(self) -> str:
        """Build a compact overview of all servers and tools (names + descriptions only)."""
        lines: list[str] = []
        for server in self.servers.values():
            lines.append(f"## {server.name}")
            lines.append(self.describe(server))
            for tool in server.tools:
                namespaced = f"{server.name}{NAMESPACE_SEP}{tool['name']}"
                desc = tool.get("description", "")
                lines.append(f"- {namespaced} — {desc}")
            lines.append("")
        return "\n".join(lines).strip()

    def get_tool_doc(self, namespaced_name: str) -> dict | None:
        """Return the full tool definition (name, description, inputSchema) for a namespaced tool."""
        result = self.resolve_tool(namespaced_name)
        if result is None:
            return None
        server, tool_name = result
        for tool in server.tools:
            if tool["name"] == tool_name:
                doc = tool.copy()
                doc["name"] = namespaced_name
                doc["server"] = server.name
                doc["server_description"] = self.describe(server)
                return doc
        return None

    def get_server_doc(self, server_name: str) -> dict | None:
        """Return full documentation for all tools on a given server."""
        server = self.servers.get(server_name)
        if server is None:
            return None
        tools = []
        for tool in server.tools:
            doc = tool.copy()
            doc["name"] = f"{server.name}{NAMESPACE_SEP}{tool['name']}"
            tools.append(doc)
        doc = {
            "server": server.name,
            "description": self.describe(server),
        }
        # Optional user-supplied note, placed before the schemas so the LLM
        # reads it first. Kept out of the compact overview on purpose.
        note = self._annotations.get_note(server.name) if self._annotations is not None else None
        if note:
            doc["notes"] = note
        doc["tools"] = tools
        return doc

    def export_registry(self) -> dict:
        """Structured registry for another Beacon to federate.

        Tool names are bare (unnamespaced) — the consumer re-namespaces them
        under its own prefix. `origin_chain` grows by one Beacon id per hop, and
        already contains this Beacon's id, so a downstream Beacon that finds its
        own id in the chain knows it is looking at a cycle.
        """
        servers = []
        for server in self.servers.values():
            chain = list(server.origin_chain)
            if self.beacon_id and self.beacon_id not in chain:
                chain.append(self.beacon_id)
            servers.append(
                {
                    "name": server.name,
                    "description": self.describe(server),
                    "tools": [
                        {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "inputSchema": t.get("inputSchema", {"type": "object", "properties": {}}),
                        }
                        for t in server.tools
                    ],
                    "notes": (self._annotations.get_note(server.name) if self._annotations else None),
                    "origin_chain": chain,
                }
            )
        return {"beacon_id": self.beacon_id, "servers": servers}

    def get_direct_tools(self) -> list[dict]:
        """Return namespaced tool dicts for tools marked as direct."""
        tools = []
        for server in self.servers.values():
            for tool in server.tools:
                if tool.get("direct"):
                    namespaced = tool.copy()
                    namespaced["name"] = f"{server.name}{NAMESPACE_SEP}{tool['name']}"
                    tools.append(namespaced)
        return tools

    def resolve_tool(self, namespaced_name: str) -> tuple[RegisteredServer, str] | None:
        """Resolve a namespaced tool name to (server, original_tool_name)."""
        parts = namespaced_name.split(NAMESPACE_SEP, 1)
        if len(parts) != 2:
            return None
        server_name, tool_name = parts
        server = self.servers.get(server_name)
        if server is None:
            return None
        return server, tool_name
