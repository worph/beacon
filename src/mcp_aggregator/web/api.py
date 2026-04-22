"""REST API and static file serving for the web UI."""

import contextlib
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from starlette.routing import Mount

from mcp_aggregator.discovery import run_discovery
from mcp_aggregator.mcp_proxy import create_mcp_session_manager
from mcp_aggregator.registry import Registry

_start_time = time.time()


def create_web_app(
    registry: Registry,
    discovery_port: int = 9099,
    public_url: str | None = None,
    auth_hash: str | None = None,
) -> FastAPI:
    session_manager = create_mcp_session_manager(registry)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async with session_manager.run():
            yield

    app = FastAPI(title="Beacon", version="0.1.0", lifespan=lifespan)

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
        try:
            responses = await run_discovery(port=discovery_port)
        except Exception as e:
            return JSONResponse(
                {"error": f"{type(e).__name__}: {e}"},
                status_code=500,
            )
        registry.update_from_discovery(responses)
        return {"discovered": len(responses), "servers": [r.name for r in responses]}

    @app.get("/api/status")
    async def status():
        total_tools = sum(len(s.tools) for s in registry.servers.values())
        hostname = os.environ.get("HOSTNAME", os.uname().nodename) or "localhost"
        web_port = int(os.environ.get("WEB_PORT", "3000"))
        return {
            "status": "ok",
            "hostname": hostname,
            "port": web_port,
            "public_url": public_url,
            "auth_hash": auth_hash,
            "uptime_seconds": round(time.time() - _start_time, 1),
            "servers": len(registry.servers),
            "tools": total_tools,
        }

    # Redirect /mcp to /mcp/ so clients work with or without trailing slash.
    # Preserve the query string so auth params like ?hash=... survive the hop
    # (nginx-hash-lock and similar proxies validate on every request).
    @app.api_route("/mcp", methods=["GET", "POST", "DELETE", "PUT"])
    async def mcp_redirect(request: Request):
        target = "/mcp/"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(url=target, status_code=307)

    # Mount MCP endpoint before static files
    app.mount("/mcp", app=session_manager.handle_request)

    # Mount static files last so API routes take precedence
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app
