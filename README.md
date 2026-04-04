# Beacon — MCP Aggregator

A local-only MCP (Model Context Protocol) aggregator that unifies multiple MCP servers behind a single endpoint. MCP servers running as Docker containers are automatically discovered on the shared network — no Docker socket required, no config files to maintain.

> **Security model:** Beacon is a local-only discovery protocol. It trusts all announcements on the Docker network unconditionally — any container on `mcp-net` can announce itself as any server with any tools. There is no authentication of discovery responses and no verification of server identity. Do not expose Beacon or `mcp-net` to untrusted networks.

## How It Works

Beacon acts as a **service mesh for MCP servers**. Instead of configuring each MCP server individually in your LLM client, you point the client at Beacon once and it handles the rest.

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│  MCP Server  │  │  MCP Server  │  │  MCP Server  │
│  (Keep)      │  │  (Lystik)    │  │  (Custom)    │
│  :9099       │  │  :80         │  │  :9099       │
└──────▲───────┘  └──────▲───────┘  └──────▲───────┘
       │ UDP announce     │                 │
       │                  │                 │
┌──────┴──────────────────┴─────────────────┴──────┐
│                   Beacon                          │
│                                                   │
│  1. Multicasts {"type":"discovery"} on UDP :9099   │
│  2. Servers respond with their tool manifests      │
│  3. Beacon registers tools with namespacing        │
│  4. Exposes 3 meta-tools on :9300/mcp/             │
│                                                   │
│  Web UI + MCP: http://localhost:9300               │
│  MCP Endpoint: http://localhost:9300/mcp/          │
└──────────────────────────────────────────────────┘
       │
       │  single MCP connection
       ▼
┌──────────────┐
│  LLM Client  │
│  (Claude, etc)│
└──────────────┘
```

### The Discovery Cycle

Every 60 seconds by default (configurable via `DISCOVERY_INTERVAL`), Beacon:

1. **Sends** a UDP discovery packet to both multicast group `239.255.99.1:9099` and broadcast `255.255.255.255:9099` on the Docker network
2. **Listens** for 2 seconds — any MCP server on the shared network that has a discovery responder (joined to the multicast group) replies with its manifest (name, description, tools, HTTP port)
3. **Rebuilds** its internal registry from the responses — new servers appear, gone servers disappear
4. **Namespaces** all tools as `{server_name}__{tool_name}` (e.g. `lystik__add_item`) to avoid name collisions

### Context-Friendly Meta-Tools

Instead of exposing every discovered tool directly (which can overwhelm the LLM context window), Beacon exposes **3 meta-tools**:

| Meta-tool | Purpose |
|---|---|
| `overview` | List all available tools with one-line descriptions, grouped by server |
| `tool_doc` | Get the full schema/description for a specific tool |
| `call` | Call a tool on a discovered server by its namespaced name |

The LLM sees only these 3 tools regardless of how many servers are discovered. It calls `overview` to discover capabilities, optionally `tool_doc` for the full schema, then `call` to invoke the tool.

**Hybrid direct mode:** Individual tools can be marked `"direct": true` in their tool definition to also appear as first-class MCP tools alongside the meta-tools. This is useful for high-frequency tools where the extra indirection would be wasteful.

### Making Your MCP Server Discoverable

An MCP server needs two things to work with Beacon:

1. **A UDP discovery responder** on port 9099 that replies to `{"type":"discovery"}` with its manifest
2. **An MCP HTTP endpoint** at `/mcp` (standard streamable HTTP transport)

SDKs are provided for both Python and Node.js (see `sdk/`) — adding discovery to an existing MCP server is ~5 lines of code.

### Docker Networking

All services must be on the same Docker bridge network. Create the shared network before starting any stack:

```bash
docker network create mcp-net
```

Any network created with `docker network create` works — Beacon uses **UDP multicast** (`239.255.99.1`) for discovery, which is supported on all Docker bridge networks. Broadcast (`255.255.255.255`) is also sent as a fallback. MCP servers don't need to expose ports to the host — all communication is container-to-container. Only Beacon maps ports to the host.

## Key Design Principles

- **Local only** — designed for a single machine; no auth, all announcements are trusted
- **Pure network discovery** — no Docker socket mount, no config files
- **Single well-known port** — `9099` for UDP discovery (internal), `9300` for MCP + Web UI (public)
- **Ephemeral registry** — servers re-announce on every discovery cycle
- **Namespaced tools** — `{server_name}__{tool_name}` avoids collisions
- **Context-friendly** — 3 meta-tools instead of N tools; scales without flooding the LLM context
- **Stack independence** — each MCP server runs from its own docker-compose; Beacon doesn't need to build or manage them

## Writing a Beacon-Compatible MCP Server

### 1. Add the Discovery Responder

**Python** (using the SDK at `sdk/python/`):

```python
from mcp_announce import create_discovery_responder

await create_discovery_responder(
    name="my-server",
    description="What this server does",
    tools=MY_TOOLS,      # same format as MCP tools/list
    port=9099,            # HTTP port where /mcp is served
    listen_port=9099,     # UDP port for discovery
)
```

**Node.js** (using the SDK at `sdk/node/`):

```javascript
const { createDiscoveryResponder } = require('./mcp-announce');

createDiscoveryResponder({
  name: 'my-server',
  description: 'What this server does',
  tools: MY_TOOLS,
  port: 80,           // HTTP port where /mcp is served
  listenPort: 9099,   // UDP port for discovery
});
```

### 2. Serve MCP at `/mcp`

Use any MCP SDK to serve the standard streamable HTTP transport. Beacon will connect to `http://<container>:<port>/mcp` to forward tool calls.

### 3. Join the Docker Network

```yaml
# In your docker-compose.yml
services:
  my-server:
    build: .
    environment:
      - DISCOVERY_PORT=9099
    networks:
      - default
      - mcp-net

networks:
  mcp-net:
    external: true
```

The `mcp-net` network must be created before starting any stack: `docker network create mcp-net`

## Ports

| Service | Port | Description |
|---|---|---|
| Beacon (public) | `localhost:9300` | Web UI + MCP endpoint at `/mcp/` |
| UDP Discovery | `9099` (internal) | Multicast + broadcast discovery on Docker network |

## Web UI

Available at `http://localhost:9300`:

- **Dashboard** — list of all discovered MCP servers and their tools
- **Connection Info** — copy-paste config for connecting LLM clients
- **Refresh** — manually trigger a discovery broadcast

## Quick Start

```bash
# Create the shared network (once)
docker network create mcp-net

# Start Beacon
cd mcp-aggregator
docker compose up -d

# Start any MCP server from its own stack (e.g. lystik)
cd ../lystik
docker compose up -d

# Open the web UI to see discovered servers
open http://localhost:9300
```

## Connecting Claude Code

Add Beacon as an MCP server — this is the only MCP config you need, regardless of how many servers are behind it:

```bash
claude mcp add beacon --transport http http://localhost:9300/mcp/
```

Or add it manually to your MCP settings (`~/.claude/settings.json` or project `.mcp.json`):

```json
{
  "mcpServers": {
    "beacon": {
      "type": "streamableHttp",
      "url": "http://localhost:9300/mcp/"
    }
  }
}
```

Once connected, all tools from all discovered servers are available in Claude. Tools are namespaced, so if Beacon discovers a server called `lystik` with a tool `add_item`, it appears as `beacon:lystik__add_item` in Claude.

### Other LLM Clients

Any client supporting MCP streamable HTTP transport can connect to:

```
http://localhost:9300/mcp/
```

## Development

```bash
# Start dev stack (Beacon + mock servers)
docker compose up -d --build

# Rebuild after code changes
docker compose up -d --build
```

## License

MIT
