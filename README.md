# MCP Aggregator

A local-only MCP (Model Context Protocol) aggregator that unifies multiple MCP servers behind a single endpoint. MCP servers running as Docker containers are automatically discovered on the shared network — no Docker socket required.

## How It Works

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│  MCP Server  │  │  MCP Server  │  │  MCP Server  │
│  (Keep)      │  │  (Lystik)    │  │  (Custom)    │
│  :9099       │  │  :9099       │  │  :9099       │
└──────▲───────┘  └──────▲───────┘  └──────▲───────┘
       │ UDP response     │                 │
       │                  │                 │
┌──────┴──────────────────┴─────────────────┴──────┐
│                MCP Aggregator                     │
│                                                   │
│  1. Broadcasts "who's here?" on UDP :9099         │
│  2. Servers respond with their tool manifests      │
│  3. Aggregator registers tools with namespacing    │
│  4. Exposes unified MCP endpoint                   │
│                                                   │
│  Web UI: http://localhost:3000                     │
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

## Key Design Principles

- **Local only** — designed for a single machine, no auth needed
- **Pure network discovery** — no Docker socket mount, no config files
- **Single well-known port** — `9099` for everything (UDP discovery + HTTP MCP)
- **Ephemeral registry** — servers re-announce on every discovery cycle
- **Namespaced tools** — `{server_name}__{tool_name}` avoids collisions

## Quick Start

```bash
# Clone and start the dev stack
git clone <repo-url>
cd mcp-aggregator
docker compose up -d

# Open the web UI
open http://localhost:3000

# Connect your LLM client to the aggregator
# MCP endpoint: http://localhost:9099/mcp (streamable HTTP)
```

## Connecting an LLM Client

### Claude Code

Add to your MCP settings:

```json
{
  "mcpServers": {
    "aggregator": {
      "type": "streamableHttp",
      "url": "http://localhost:9099/mcp"
    }
  }
}
```

### Other LLM Clients

Any client supporting MCP streamable HTTP transport can connect to:

```
http://localhost:9099/mcp
```

## Writing an MCP Server Compatible with the Aggregator

Your MCP server container needs two things on port `9099`:

### 1. UDP Discovery Listener

Listen on UDP port `9099`. When you receive a `{"type":"discovery"}` packet, respond with your manifest:

```json
{
  "name": "my-server",
  "description": "What this server does",
  "tools": [
    {
      "name": "my_tool",
      "description": "What this tool does",
      "inputSchema": {
        "type": "object",
        "properties": {
          "arg1": { "type": "string", "description": "First argument" }
        },
        "required": ["arg1"]
      }
    }
  ]
}
```

### 2. MCP Streamable HTTP Endpoint

Serve the standard MCP streamable HTTP transport on `http://<container>:9099/mcp`.

### Example Docker Compose

```yaml
services:
  my-mcp-server:
    image: my-mcp-server:latest
    networks:
      - mcp-net

networks:
  mcp-net:
    external: true
    name: mcp-net
```

### SDK / Helper Library

A minimal helper library (`mcp-aggregator-sdk`) will be provided to handle the UDP listener boilerplate. Adding aggregator compatibility to an existing MCP server becomes ~5 lines of code.

## Web UI

Available at `http://localhost:3000`:

- **Dashboard** — list of all discovered MCP servers and their tools
- **Connection Info** — copy-paste config for connecting LLM clients
- **Refresh** — manually trigger a discovery broadcast

## Development

```bash
# Start dev stack with hot reload
docker compose -f docker-compose.dev.yml up

# Aggregator with live reload on http://localhost:3000 (UI) and :9099 (MCP)
# Includes two mock MCP servers for testing
```

## Architecture

See [IMPLEMENTATION.md](./IMPLEMENTATION.md) for detailed technical notes.

## License

MIT
