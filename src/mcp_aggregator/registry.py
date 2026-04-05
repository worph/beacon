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
    path: str = "/mcp"
    auth: dict | None = None
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
                path=resp.path,
                auth=resp.auth,
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

    def get_instructions(self) -> str:
        """Build server instructions with a one-liner per discovered server."""
        lines = ["Beacon MCP aggregator. Call server_doc with a server name to get full tool schemas for that server.", ""]
        lines.append("Available servers:")
        for server in self.servers.values():
            lines.append(f"- {server.name} — {server.description}")
        return "\n".join(lines)

    def get_overview_text(self) -> str:
        """Build a compact overview of all servers and tools (names + descriptions only)."""
        lines: list[str] = []
        for server in self.servers.values():
            lines.append(f"## {server.name}")
            lines.append(server.description)
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
                doc["server_description"] = server.description
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
        return {
            "server": server.name,
            "description": server.description,
            "tools": tools,
        }

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
