"""REST API and static file serving for the web UI."""

import contextlib
import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from mcp_aggregator.discovery import run_discovery
from mcp_aggregator.external import ExternalConfig, ExternalManager
from mcp_aggregator.mcp_proxy import create_mcp_session_manager
from mcp_aggregator.registry import Registry

logger = logging.getLogger(__name__)

_start_time = time.time()


def _server_dict(s) -> dict:
    return {
        "name": s.name,
        "description": s.description,
        "ip": s.ip,
        "port": s.port,
        "path": s.path,
        "url": s.url,
        "tools": s.tools,
        "authenticated": s.auth is not None or bool(s.headers),
        "origin": s.origin,
        "last_seen": s.last_seen,
        "error": s.error,
    }


def _parse_external_payload(payload: dict) -> list[ExternalConfig]:
    """Accept either a single server `{name, url, headers?, description?}` or a
    bundle `{mcpServers: {name: {url, headers?, type?, description?}}}`."""
    if "mcpServers" in payload:
        bundle = payload.get("mcpServers") or {}
        if not isinstance(bundle, dict):
            raise ValueError("mcpServers must be an object")
        out = []
        for name, entry in bundle.items():
            if not isinstance(entry, dict):
                raise ValueError(f"{name}: entry must be an object")
            url = entry.get("url")
            if not url:
                raise ValueError(f"{name}: missing url")
            headers = entry.get("headers") or {}
            if not isinstance(headers, dict):
                raise ValueError(f"{name}: headers must be an object")
            out.append(ExternalConfig(
                name=name,
                url=url,
                headers={str(k): str(v) for k, v in headers.items()},
                description=entry.get("description", "") or "",
            ))
        return out

    name = payload.get("name")
    url = payload.get("url")
    if not name or not url:
        raise ValueError("Request must include `name` and `url`, or an `mcpServers` bundle")
    headers = payload.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValueError("headers must be an object")
    return [ExternalConfig(
        name=str(name),
        url=str(url),
        headers={str(k): str(v) for k, v in headers.items()},
        description=str(payload.get("description") or ""),
    )]


def create_web_app(
    registry: Registry,
    external_manager: ExternalManager,
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
        return [_server_dict(s) for s in registry.servers.values()]

    @app.get("/api/servers/{name}")
    async def get_server(name: str):
        server = registry.servers.get(name)
        if server is None:
            return JSONResponse({"error": "Server not found"}, status_code=404)
        return _server_dict(server)

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
        await external_manager.refresh_all()
        return {"discovered": len(responses), "servers": [r.name for r in responses]}

    @app.get("/api/external")
    async def list_external():
        # Redact headers in responses — they often contain secrets (bearer tokens).
        return [
            {
                "name": c.name,
                "url": c.url,
                "description": c.description,
                "header_keys": list(c.headers.keys()),
            }
            for c in external_manager.list_configs()
        ]

    @app.post("/api/external")
    async def add_external(request: Request):
        try:
            payload = await request.json()
        except Exception as e:
            return JSONResponse({"error": f"Invalid JSON: {e}"}, status_code=400)
        try:
            configs = _parse_external_payload(payload)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        added: list[dict] = []
        for cfg in configs:
            external_manager.upsert(cfg)
            await external_manager.refresh_one(cfg)
            srv = registry.servers.get(cfg.name)
            added.append({
                "name": cfg.name,
                "tools": len(srv.tools) if srv else 0,
                "error": srv.error if srv else None,
            })
        return {"added": added}

    @app.delete("/api/external/{name}")
    async def delete_external(name: str):
        removed = external_manager.remove(name)
        if not removed:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return {"removed": name}

    @app.post("/api/external/refresh")
    async def refresh_external():
        await external_manager.refresh_all()
        return {"refreshed": len(external_manager.configs)}

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
