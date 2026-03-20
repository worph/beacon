"""REST API and static file serving for the web UI."""

import os
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from mcp_aggregator.discovery import run_discovery
from mcp_aggregator.registry import Registry

_start_time = time.time()


def create_web_app(registry: Registry, discovery_port: int = 9099) -> FastAPI:
    app = FastAPI(title="Beacon", version="0.1.0")

    @app.get("/api/servers")
    async def list_servers():
        return [
            {
                "name": s.name,
                "description": s.description,
                "ip": s.ip,
                "port": s.port,
                "path": s.path,
                "tools": s.tools,
                "authenticated": s.auth is not None,
                "last_seen": s.last_seen,
            }
            for s in registry.servers.values()
        ]

    @app.get("/api/servers/{name}")
    async def get_server(name: str):
        server = registry.servers.get(name)
        if server is None:
            return JSONResponse({"error": "Server not found"}, status_code=404)
        return {
            "name": server.name,
            "description": server.description,
            "ip": server.ip,
            "port": server.port,
            "path": server.path,
            "tools": server.tools,
            "authenticated": server.auth is not None,
            "last_seen": server.last_seen,
        }

    @app.post("/api/discover")
    async def trigger_discovery():
        responses = await run_discovery(port=discovery_port)
        registry.update_from_discovery(responses)
        return {"discovered": len(responses), "servers": [r.name for r in responses]}

    @app.get("/api/status")
    async def status():
        total_tools = sum(len(s.tools) for s in registry.servers.values())
        hostname = os.environ.get("HOSTNAME", os.uname().nodename) or "localhost"
        return {
            "status": "ok",
            "hostname": hostname,
            "uptime_seconds": round(time.time() - _start_time, 1),
            "servers": len(registry.servers),
            "tools": total_tools,
        }

    # Mount static files last so API routes take precedence
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app
