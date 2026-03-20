"""Registry of discovered MCP servers and their namespaced tools."""

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
    ip: str
    port: int
    tools: list[dict]
    last_seen: float = field(default_factory=time.time)


class Registry:
    """Stores discovered MCP servers and provides namespaced tool lookups."""

    def __init__(self) -> None:
        self.servers: dict[str, RegisteredServer] = {}

    def update_from_discovery(self, responses: list[DiscoveryResponse]) -> None:
        """Full replace of registry from discovery responses."""
        now = time.time()
        new_servers: dict[str, RegisteredServer] = {}
        for resp in responses:
            new_servers[resp.name] = RegisteredServer(
                name=resp.name,
                description=resp.description,
                ip=resp.ip,
                port=resp.port,
                tools=resp.tools,
                last_seen=now,
            )
        added = set(new_servers) - set(self.servers)
        removed = set(self.servers) - set(new_servers)
        if added:
            logger.info("New servers: %s", added)
        if removed:
            logger.info("Removed servers: %s", removed)
        self.servers = new_servers

    def get_all_namespaced_tools(self) -> list[dict]:
        """Return all tools with namespace-prefixed names."""
        tools = []
        for server in self.servers.values():
            for tool in server.tools:
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
