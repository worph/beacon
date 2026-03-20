# Beacon — MCP Aggregator

A local-only MCP (Model Context Protocol) aggregator that unifies multiple MCP servers behind a single endpoint. MCP servers running as Docker containers are automatically discovered on the shared network — no Docker socket required, no config files to maintain.

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
│  1. Broadcasts {"type":"discovery"} on UDP :9099  │
│  2. Servers respond with their tool manifests      │
│  3. Beacon registers tools with namespacing        │
│  4. Exposes unified MCP endpoint on :9099/mcp      │
│                                                   │
│  Web UI:       http://localhost:9300               │
│  MCP Endpoint: http://localhost:9099/mcp           │
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

Every 30 seconds (configurable via `DISCOVERY_INTERVAL`), Beacon:

1. **Broadcasts** a UDP packet `{"type":"discovery"}` to `255.255.255.255:9099` on the Docker network
2. **Listens** for 2 seconds — any MCP server on the `mcp-net` network that has a discovery responder replies with its manifest (name, description, tools, HTTP port)
3. **Rebuilds** its internal registry from the responses — new servers appear, gone servers disappear
4. **Namespaces** all tools as `{server_name}__{tool_name}` (e.g. `lystik__add_item`) to avoid name collisions

When an LLM client calls a tool through Beacon's MCP endpoint, Beacon resolves the namespace, opens a fresh HTTP connection to the target server's `/mcp` endpoint, forwards the JSON-RPC call, and returns the result.

### Making Your MCP Server Discoverable

An MCP server needs two things to work with Beacon:

1. **A UDP discovery responder** on port 9099 that replies to `{"type":"discovery"}` with its manifest
2. **An MCP HTTP endpoint** at `/mcp` (standard streamable HTTP transport)

SDKs are provided for both Python and Node.js (see `sdk/`) — adding discovery to an existing MCP server is ~5 lines of code.

### Docker Networking

All services must join the shared `mcp-net` bridge network. Beacon creates this network; other stacks reference it as `external: true`. MCP servers don't need to expose ports to the host — all communication is container-to-container. Only Beacon maps ports to the host.

## Key Design Principles

- **Local only** — designed for a single machine, no auth needed
- **Pure network discovery** — no Docker socket mount, no config files
- **Single well-known port** — `9099` for UDP discovery + HTTP MCP
- **Ephemeral registry** — servers re-announce on every discovery cycle
- **Namespaced tools** — `{server_name}__{tool_name}` avoids collisions
- **Stack independence** — each MCP server runs from its own docker-compose; Beacon doesn't need to build or manage them

## Quick Start

```bash
# Start Beacon (creates the mcp-net network)
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
claude mcp add beacon --transport http http://localhost:9099/mcp
```

Or add it manually to your MCP settings (`~/.claude/settings.json` or project `.mcp.json`):

```json
{
  "mcpServers": {
    "beacon": {
      "type": "streamableHttp",
      "url": "http://localhost:9099/mcp"
    }
  }
}
```

Once connected, all tools from all discovered servers are available in Claude. Tools are namespaced, so if Beacon discovers a server called `lystik` with a tool `add_item`, it appears as `beacon:lystik__add_item` in Claude.

### Other LLM Clients

Any client supporting MCP streamable HTTP transport can connect to:

```
http://localhost:9099/mcp
```

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

The `mcp-net` network is created by Beacon's stack. Start Beacon first, then your server.

## Ports

| Service | Port | Description |
|---|---|---|
| Beacon MCP | `localhost:9099` | MCP streamable HTTP endpoint (`/mcp`) |
| Beacon Web UI | `localhost:9300` | Dashboard, server list, connection info |
| UDP Discovery | `9099` (internal) | Broadcast discovery on Docker network |

## Web UI

Available at `http://localhost:9300`:

- **Dashboard** — list of all discovered MCP servers and their tools
- **Connection Info** — copy-paste config for connecting LLM clients
- **Refresh** — manually trigger a discovery broadcast

## Development

```bash
# Start dev stack (Beacon + mock servers)
docker compose up -d --build

# Rebuild after code changes
docker compose up -d --build
```

## Architecture

See [IMPLEMENTATION.md](./IMPLEMENTATION.md) for detailed technical notes.

## License

MIT
