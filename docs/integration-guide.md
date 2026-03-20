# Beacon Integration Guide

How to make your MCP server discoverable by the Beacon aggregator.

## Overview

Beacon discovers MCP servers via UDP broadcast on a shared Docker network. Your server needs to do two things:

1. **Listen for UDP discovery requests** on port `9099` and respond with a manifest
2. **Serve MCP over HTTP** at `/mcp` on port `9099` (streamable HTTP transport)

You can implement the UDP protocol directly (~30 lines) or use the provided Python SDK.

## Quick Start (Python SDK)

### Install

```bash
pip install /path/to/sdk/python/
# or in a Dockerfile:
COPY sdk/python/ /app/sdk/python/
RUN pip install /app/sdk/python/
```

### Usage

```python
import asyncio
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp_announce import create_discovery_responder

# 1. Create your MCP server
mcp = FastMCP(
    "my-server",
    # Required: disable DNS rebinding protection for container-to-container calls
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    # Required: stateless mode for aggregator proxy compatibility
    stateless_http=True,
)

# 2. Define your tools
@mcp.tool()
def hello(name: str) -> str:
    """Say hello."""
    return f"Hello, {name}!"

# 3. Declare tool schemas for discovery
TOOL_DEFS = [
    {
        "name": "hello",
        "description": "Say hello.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name to greet"},
            },
            "required": ["name"],
        },
    },
]

# 4. Run both UDP responder and HTTP server
async def main():
    transport = await create_discovery_responder(
        name="my-server",           # Unique server name (used in tool namespacing)
        description="My MCP server",
        tools=TOOL_DEFS,            # Tool schemas for discovery
        port=9099,                  # HTTP port (must match uvicorn)
        listen_port=9099,           # UDP port (must be 9099)
    )

    app = mcp.streamable_http_app()
    config = uvicorn.Config(app, host="0.0.0.0", port=9099, log_level="info")
    server = uvicorn.Server(config)

    try:
        await server.serve()
    finally:
        transport.close()

if __name__ == "__main__":
    asyncio.run(main())
```

### Dockerfile

```dockerfile
FROM python:3.13-slim

WORKDIR /app
RUN pip install --no-cache-dir mcp>=1.26.0 uvicorn[standard]

# Install the discovery SDK
COPY sdk/python/ /app/sdk/python/
RUN pip install --no-cache-dir /app/sdk/python/

COPY server.py .

EXPOSE 9099/tcp
EXPOSE 9099/udp

CMD ["python", "server.py"]
```

### docker-compose.yml

Add your server to the `mcp-net` network alongside the aggregator:

```yaml
services:
  aggregator:
    # ... existing aggregator config ...
    networks:
      - mcp-net

  my-server:
    build: ./my-server
    networks:
      - mcp-net

networks:
  mcp-net:
    driver: bridge
```

## UDP Discovery Protocol

If you're not using Python, implement the protocol directly. It's two UDP messages.

### Discovery Request (from aggregator)

The aggregator broadcasts this JSON to `255.255.255.255:9099`:

```json
{"type": "discovery"}
```

### Announce Response (from your server)

Your server sends this JSON back as a unicast reply to the sender's address:

```json
{
  "type": "announce",
  "name": "my-server",
  "description": "What this server does",
  "port": 9099,
  "tools": [
    {
      "name": "tool_name",
      "description": "What this tool does",
      "inputSchema": {
        "type": "object",
        "properties": {
          "param1": {"type": "string", "description": "A parameter"}
        },
        "required": ["param1"]
      }
    }
  ]
}
```

### Field Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | yes | Must be `"announce"` |
| `name` | string | yes | Unique server name. Used for tool namespacing (`{name}__{tool}`). Use lowercase with hyphens. |
| `description` | string | no | Human-readable description shown in the web UI |
| `port` | integer | no | HTTP port serving MCP (default: `9099`) |
| `tools` | array | no | Tool definitions following MCP's Tool schema |

### Tool Schema

Each tool in the `tools` array follows the MCP Tool format:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Tool name (no namespace prefix — aggregator adds it) |
| `description` | string | yes | What the tool does |
| `inputSchema` | object | yes | JSON Schema for the tool's input parameters |

## MCP Server Requirements

Your MCP server must:

1. **Serve on `/mcp`** — the aggregator connects to `http://{ip}:{port}/mcp`
2. **Use streamable HTTP transport** — SSE and stdio are not supported
3. **Disable DNS rebinding protection** — the aggregator connects by container IP, not `localhost`
4. **Use stateless HTTP mode** — the aggregator creates a fresh connection per tool call

## How Tool Namespacing Works

When the aggregator discovers your server named `my-server` with a tool called `hello`, it exposes that tool as `my-server__hello` (double underscore separator).

LLM clients see and call the namespaced name. The aggregator strips the prefix and proxies the call to your server using the original tool name.

**Naming rules:**
- Server names must be unique across all discovered servers
- Tool names must be unique within a single server
- Avoid `__` in server names or tool names

## Implementing in Other Languages

The protocol is language-agnostic. You need:

1. A UDP socket listening on port `9099` that responds to `{"type":"discovery"}` with your announce JSON
2. An MCP server using streamable HTTP transport on port `9099` at path `/mcp`

### Go Example (UDP responder only)

```go
func listenDiscovery(manifest []byte) {
    addr, _ := net.ResolveUDPAddr("udp", ":9099")
    conn, _ := net.ListenUDP("udp", addr)
    defer conn.Close()

    buf := make([]byte, 1024)
    for {
        n, remote, _ := conn.ReadFromUDP(buf)
        var msg map[string]string
        json.Unmarshal(buf[:n], &msg)
        if msg["type"] == "discovery" {
            conn.WriteToUDP(manifest, remote)
        }
    }
}
```

### Node.js Example (UDP responder only)

```javascript
const dgram = require('dgram');
const server = dgram.createSocket('udp4');

const manifest = JSON.stringify({
  type: 'announce',
  name: 'my-server',
  description: 'My MCP server',
  port: 9099,
  tools: [/* ... */],
});

server.on('message', (msg, rinfo) => {
  const data = JSON.parse(msg);
  if (data.type === 'discovery') {
    server.send(manifest, rinfo.port, rinfo.address);
  }
});

server.bind(9099);
```

## Environment Variables

The SDK respects no environment variables, but the mock servers use these conventions:

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_PORT` | `9099` | HTTP port for MCP server |
| `DISCOVERY_PORT` | `9099` | UDP port for discovery |

## Troubleshooting

**Server not discovered:**
- Verify your container is on the same Docker network as the aggregator (`mcp-net`)
- Check that UDP port 9099 is exposed in your Dockerfile
- Trigger manual discovery: `curl -X POST http://localhost:3000/api/discover`

**421 Misdirected Request when aggregator calls tools:**
- You need `transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)` in your FastMCP config
- The aggregator connects by container IP, which won't match the default `localhost` allowlist

**Tools listed but calls fail:**
- Ensure `stateless_http=True` in your FastMCP config
- Verify your MCP server is at `/mcp` (the default for FastMCP)

**Tool schema mismatch:**
- The `TOOL_DEFS` you pass to `create_discovery_responder` are what the aggregator shows to clients
- These must match your actual `@mcp.tool()` definitions — mismatches will cause call failures
