"""REST API and static file serving for the web UI."""

import contextlib
import json
import logging
import os
import re
import time
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from mcp_aggregator.annotations import AnnotationStore
from mcp_aggregator.discovery import run_discovery
from mcp_aggregator.external import ExternalConfig, ExternalManager, config_from_entry
from mcp_aggregator.mcp_proxy import META_TOOLS, create_mcp_session_manager
from mcp_aggregator.oauth import BeaconOAuthManager
from mcp_aggregator.registry import Registry

logger = logging.getLogger(__name__)

_start_time = time.time()


def _server_dict(s, override: str | None = None, note: str | None = None) -> dict:
    return {
        "name": s.name,
        # `description` is always the discovered/external default (the restore baseline);
        # `description_override` is the user-supplied text in effect, or None.
        "description": s.description,
        "description_override": override,
        # `note` is optional extra text injected into this server's server_doc.
        "note": note,
        "ip": s.ip,
        "port": s.port,
        "path": s.path,
        "url": s.url,
        "tools": s.tools,
        "authenticated": s.auth is not None or bool(s.headers),
        "origin": s.origin,
        "last_seen": s.last_seen,
        "error": s.error,
        # Federation: set when this server is reached through another Beacon.
        "federated_via": s.federated_via,
        "remote_name": s.remote_name,
        "auth_required": s.auth_required,
    }


def _derive_name(url: str) -> str:
    """Best-effort server name from a URL, for the add-by-URL flow.

    `https://beacon-yunderalabs.nsl.sh/mcp` -> `beacon-yunderalabs`. The user can
    override it; this only has to be reasonable and stable.
    """
    host = urlparse(url).hostname or ""
    label = host.split(".")[0] if host else "external"
    label = re.sub(r"[^A-Za-z0-9_.-]", "-", label).strip("-.")
    return label or "external"


def _parse_external_payload(payload: dict) -> list[ExternalConfig]:
    """Accept a single server or a bundle.

    Single: `{url, name?, headers?, description?, scopes?, federate?, ...}` —
    `name` is optional and derived from the URL when omitted, which is what makes
    "paste one URL" work for OAuth servers.
    Bundle: `{mcpServers: {name: {url, ...}}}`.
    """
    if "mcpServers" in payload:
        bundle = payload.get("mcpServers") or {}
        if not isinstance(bundle, dict):
            raise ValueError("mcpServers must be an object")
        out = []
        for name, entry in bundle.items():
            if not isinstance(entry, dict):
                raise ValueError(f"{name}: entry must be an object")
            out.append(config_from_entry(name, entry))
        return out

    url = payload.get("url")
    if not url:
        raise ValueError("Request must include `url`, or an `mcpServers` bundle")
    name = payload.get("name") or _derive_name(str(url))
    entry = {k: v for k, v in payload.items() if k != "name"}
    return [config_from_entry(str(name), entry)]


def _callback_page(message: str, name: str | None, ok: bool) -> str:
    """Tiny self-closing page shown in the OAuth popup.

    It notifies the opener so the UI can refresh immediately instead of waiting
    for the next poll, then closes itself.
    """
    colour = "#2e7d32" if ok else "#c62828"
    safe = message.replace("<", "&lt;").replace(">", "&gt;")
    payload = f'{{"source":"beacon-oauth","ok":{"true" if ok else "false"},"name":{json.dumps(name)}}}'
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Beacon — OAuth</title></head>
<body style="font-family:system-ui,sans-serif;padding:2rem;text-align:center">
  <p style="color:{colour};font-size:1.1rem">{safe}</p>
  <script>
    try {{ window.opener && window.opener.postMessage({payload}, "*"); }} catch (e) {{}}
    if ({"true" if ok else "false"}) setTimeout(function () {{ window.close(); }}, 1200);
  </script>
</body></html>"""


def create_web_app(
    registry: Registry,
    external_manager: ExternalManager,
    annotations: AnnotationStore,
    oauth_manager: BeaconOAuthManager | None = None,
    discovery_port: int = 9099,
    public_url: str | None = None,
    auth_hash: str | None = None,
    oauth_admin_url: str | None = None,
) -> FastAPI:
    oauth = oauth_manager or external_manager.oauth
    session_manager = create_mcp_session_manager(registry, external_manager.auth_for_server)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async with session_manager.run():
            yield

    app = FastAPI(title="Beacon", version="0.1.0", lifespan=lifespan)

    @app.get("/api/servers")
    async def list_servers():
        return [
            _server_dict(s, annotations.get(s.name), annotations.get_note(s.name))
            for s in registry.servers.values()
        ]

    @app.get("/api/servers/{name}")
    async def get_server(name: str):
        server = registry.servers.get(name)
        if server is None:
            return JSONResponse({"error": "Server not found"}, status_code=404)
        return _server_dict(server, annotations.get(server.name), annotations.get_note(server.name))

    @app.get("/api/servers/{name}/doc")
    async def get_server_doc(name: str):
        # The exact object the `server_doc` meta-tool returns to the LLM for this
        # server (description override + note applied), for the UI "i" preview.
        doc = registry.get_server_doc(name)
        if doc is None:
            return JSONResponse({"error": "Server not found"}, status_code=404)
        return doc

    @app.get("/api/beacon-info")
    async def beacon_info():
        # What Beacon itself exposes: the top-level instructions string returned in
        # the MCP initialize response, plus the meta-tools exposed directly.
        return {
            "instructions": registry.get_instructions(),
            "tools": [
                {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
                for t in META_TOOLS
            ],
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
        await external_manager.refresh_all()
        return {"discovered": len(responses), "servers": [r.name for r in responses]}

    @app.get("/api/external")
    async def list_external():
        # Redact headers in responses — they often contain secrets (bearer tokens).
        out = []
        for c in external_manager.list_configs():
            group = registry.external_group(c.name)
            out.append({
                "name": c.name,
                "url": c.url,
                "description": c.description,
                "header_keys": list(c.headers.keys()),
                "oauth": c.oauth,
                "scopes": c.scopes,
                "federate": c.federate,
                # Presence only — the secret is never returned, same policy as headers.
                "has_client_credentials": bool(c.client_id),
                "auth": oauth.summary(c.name) if c.oauth else {"status": "none"},
                # Names this config contributes to the registry: itself for a
                # plain server, one per remote server once federated.
                "servers": [s.name for s in group],
                "federated": [s.name for s in group if s.federated_via],
                "error": next((s.error for s in group if s.error), None),
            })
        return out

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
            group = registry.external_group(cfg.name)
            needs_auth = any(s.auth_required for s in group)
            added.append({
                "name": cfg.name,
                "tools": sum(len(s.tools) for s in group),
                "servers": [s.name for s in group],
                "error": next((s.error for s in group if s.error), None),
                # The UI turns this into a "Connect" button rather than an error.
                "auth_required": needs_auth,
            })
        return {"added": added}

    @app.delete("/api/external/{name}")
    async def delete_external(name: str):
        removed = external_manager.remove(name)
        if not removed:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return {"removed": name}

    @app.post("/api/external/{name}/authorize")
    async def authorize_external(name: str):
        cfg = external_manager.get(name)
        if cfg is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        try:
            url = await external_manager.authorize(cfg)
        except Exception as e:
            logger.error("Authorization for %r failed to start: %s", name, e)
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"name": name, "authorize_url": url, "redirect_uri": oauth.redirect_uri}

    @app.delete("/api/external/{name}/authorize")
    async def deauthorize_external(name: str):
        cfg = external_manager.get(name)
        if cfg is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        cleared = oauth.disconnect(name)
        oauth.mark_needs_auth(name)
        await external_manager.refresh_one(cfg)
        return {"name": name, "cleared": cleared}

    @app.get("/api/oauth/callback")
    async def oauth_callback(request: Request):
        """Redirect target for every external server's OAuth flow.

        Resolves the pending authorization keyed by `state`, then closes itself;
        the real work continues in the background task that started the flow.
        """
        params = request.query_params
        state = params.get("state") or ""
        error = params.get("error")
        code = params.get("code")

        if error:
            name = oauth.fail_callback(state, f"{error}: {params.get('error_description', '')}")
            return HTMLResponse(_callback_page(f"Authorization failed: {error}", name, ok=False))
        if not code or not state:
            return HTMLResponse(
                _callback_page("Missing code or state in the OAuth redirect.", None, ok=False),
                status_code=400,
            )
        name = oauth.complete_callback(state, code)
        if name is None:
            return HTMLResponse(
                _callback_page(
                    "No authorization is waiting for this response — it may have timed out.",
                    None, ok=False,
                ),
                status_code=400,
            )
        return HTMLResponse(_callback_page(f"Connected {name}. You can close this window.", name, ok=True))

    @app.post("/api/external/refresh")
    async def refresh_external():
        await external_manager.refresh_all()
        return {"refreshed": len(external_manager.configs)}

    @app.get("/api/annotations")
    async def list_annotations():
        return {
            "instructions_note": annotations.get_instructions_note(),
            "descriptions": annotations.all(),
            "server_notes": annotations.all_notes(),
        }

    @app.get("/api/instructions-note")
    async def get_instructions_note():
        return {"note": annotations.get_instructions_note()}

    @app.put("/api/instructions-note")
    async def set_instructions_note(request: Request):
        try:
            payload = await request.json()
        except Exception as e:
            return JSONResponse({"error": f"Invalid JSON: {e}"}, status_code=400)
        note = payload.get("note") if isinstance(payload, dict) else None
        if not isinstance(note, str) and note is not None:
            return JSONResponse({"error": "`note` must be a string"}, status_code=400)
        active = annotations.set_instructions_note(note or "")
        return {"note": annotations.get_instructions_note(), "cleared": not active}

    @app.put("/api/annotations/{name}")
    async def set_annotation(name: str, request: Request):
        try:
            payload = await request.json()
        except Exception as e:
            return JSONResponse({"error": f"Invalid JSON: {e}"}, status_code=400)
        if not isinstance(payload, dict) or ("description" not in payload and "note" not in payload):
            return JSONResponse(
                {"error": "Body must include `description` and/or `note`"}, status_code=400
            )
        if "description" in payload:
            description = payload.get("description") or ""
            if not isinstance(description, str):
                return JSONResponse({"error": "`description` must be a string"}, status_code=400)
            annotations.set(name, description)
        if "note" in payload:
            note = payload.get("note") or ""
            if not isinstance(note, str):
                return JSONResponse({"error": "`note` must be a string"}, status_code=400)
            annotations.set_note(name, note)
        return {
            "name": name,
            "description": annotations.get(name) or "",
            "note": annotations.get_note(name) or "",
        }

    @app.delete("/api/annotations/{name}")
    async def delete_annotation(name: str):
        annotations.remove(name)
        return {"restored": name}

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
            "oauth_admin_url": oauth_admin_url,
            # Redirect URI registered with remote authorization servers. Shown in
            # the UI so a mismatch with the browser's origin is obvious.
            "oauth_redirect_uri": oauth.redirect_uri,
            "beacon_id": registry.beacon_id,
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
