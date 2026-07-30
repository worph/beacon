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

### External (non-Beacon-ready) MCP Servers

Not every MCP server you want to use is going to run in your Docker network with the Beacon SDK embedded — many are SaaS-hosted HTTP endpoints (n8n, Zapier, hosted GitHub MCP, etc.).

Beacon supports these as **external servers**. Instead of discovery, Beacon connects to them directly over MCP streamable HTTP using a URL + optional headers, polls `list_tools`, and proxies calls through the same `call` meta-tool as discovered servers. Configs are persisted to disk so they survive restarts.

**Add via the Web UI** — expand **External MCP Servers** and paste a Claude Desktop `mcpServers` bundle:

```json
{
  "mcpServers": {
    "n8n-mcp": {
      "type": "http",
      "url": "https://n8n.example.com/mcp-server/http",
      "headers": {
        "Authorization": "Bearer eyJ..."
      }
    }
  }
}
```

**Or via the REST API** — `POST /api/external` with the same JSON, or with a single `{"name": ..., "url": ..., "headers": {...}}` object. `GET /api/external` lists configured servers (header *values* are redacted, only key names are returned). `DELETE /api/external/{name}` removes one.

### OAuth-Protected Servers

Many hosted MCP endpoints don't take a pasted token at all — they want an OAuth 2.1 authorization flow. For those, paste **just the URL** into the add form (or `POST /api/external` with `{"url": "..."}`):

```bash
curl -X POST http://localhost:9300/api/external \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://beacon.example.com/mcp"}'
```

Beacon connects, sees the `401`, and marks the server **needs auth**. Click **Connect** and it runs the whole flow — discovers the authorization server, registers itself as a client dynamically, and opens the login in a popup. When you finish logging in, the tokens are stored and refreshed automatically from then on.

Tokens live in `/app/data/oauth/{name}.json` (mode `0600`) and survive restarts, refreshing silently when the access token expires.

Most servers need nothing else. **Advanced settings** on the add form covers the exceptions:

| Field | When you need it |
|---|---|
| **OAuth Client ID** / **Client Secret** | The server doesn't allow dynamic client registration and gave you credentials to use instead. Leave empty otherwise — Beacon registers itself. |
| **OAuth Scopes** | The server under-advertises its scopes. Beacon then registers *and* requests exactly what you put here. |

The client secret is stored server-side and never returned by the API — `GET /api/external` reports only `has_client_credentials`.

The redirect URI Beacon registers is shown in the UI. It defaults to `http://localhost:{WEB_PORT}/api/oauth/callback`; set `OAUTH_REDIRECT_BASE_URL` if you reach the UI on a different origin, since the authorization server sends the browser back to the URI that was registered.

If the remote issues no refresh token (some advertise only their own scope, so no client ever asks for offline access), set **OAuth Scopes** to `<their-scope> offline_access`. Beacon always adds `prompt=consent` when offline access is requested — OpenID Connect Core 11.1 requires it, and without it the authorization server silently drops the scope and returns a token that cannot be renewed.

### Federating Another Beacon

Point Beacon at another Beacon and it doesn't nest it — it **imports** it. A remote Beacon only exposes its four meta-tools, so nesting would hide everything behind it. Instead each remote server is registered locally under a prefix:

```
yunderalabs.docmost-mcp__search_pages
yunderalabs.n8n-mcp__list_workflows
```

`overview`, `tool_doc` and `server_doc` work on them exactly like local servers, and `call` transparently routes through the remote. Combined with the OAuth support above, adding a remote Beacon is one URL and one login.

Beacons stamp an instance id into what they export, so two Beacons pointed at each other detect the cycle and skip the servers that would loop. Set `"federate": "off"` on a config to keep a remote Beacon nested instead.

> ⚠ Beacon does not authenticate its own callers. Federating a remote makes everything behind it reachable by anyone who can reach your `/mcp/` — keep it on localhost.

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

### Remote Connections (behind an auth proxy)

When Beacon sits behind an HTTPS proxy with hash-based auth (e.g. `nginx-hash-lock`), set `PUBLIC_URL` and `AUTH_HASH` on the Beacon container and the Web UI will render a ready-to-paste `claude mcp add` command with `?hash=...` appended:

```yaml
environment:
  PUBLIC_URL: "https://beacon.example.com/mcp"
  AUTH_HASH: "your-hash-token"
```

Note that `/api/oauth/callback` must stay reachable *without* the proxy's own auth — a remote authorization server redirects the browser back to the URI Beacon registered, and it will not carry a `?hash=`.

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `DISCOVERY_PORT` | `9099` | UDP port for discovery |
| `DISCOVERY_INTERVAL` | `60` | Seconds between discovery cycles |
| `WEB_PORT` | `3000` | HTTP port for Web UI + MCP endpoint |
| `MCP_URL` | `http://beacon:9300/mcp` | URL advertised to servers |
| `MCP_CLIENT_TIMEOUT` | `300` | HTTP read timeout (seconds) for proxied tool calls |
| `EXTERNAL_CONFIG_PATH` | `/app/data/external.json` | Where external server configs are persisted |
| `EXTERNAL_POLL_INTERVAL` | `60` | Seconds between external tool-list refreshes |
| `ANNOTATIONS_CONFIG_PATH` | `/app/data/annotations.json` | Where description overrides and notes are persisted |
| `OAUTH_CONFIG_DIR` | `/app/data/oauth` | Per-server OAuth tokens + registered client (`0600`) |
| `OAUTH_REDIRECT_BASE_URL` | _(derived)_ | Browser-reachable base for the OAuth callback; falls back to the origin of `PUBLIC_URL`, then `http://localhost:{WEB_PORT}` |
| `BEACON_ID_PATH` | `/app/data/beacon-id` | Stable instance id used as the federation loop guard |
| `PUBLIC_URL` | _(unset)_ | Externally reachable MCP URL |
| `AUTH_HASH` | _(unset)_ | Hash token appended to `PUBLIC_URL` in the UI |
| `OAUTH_ADMIN_URL` | _(unset)_ | Link to an external OAuth admin page (inbound auth; unrelated to the outbound OAuth above) |
| `LOG_LEVEL` | `info` | Logging level |

## Development

```bash
# Start dev stack (Beacon + mock servers)
docker compose up -d --build

# Rebuild after code changes
docker compose up -d --build
```

## License

MIT
