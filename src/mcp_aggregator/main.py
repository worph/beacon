"""Main entrypoint — runs MCP proxy and web UI concurrently."""

import asyncio
import logging
import os

import uvicorn

from mcp_aggregator.annotations import AnnotationStore
from mcp_aggregator.discovery import run_discovery
from mcp_aggregator.external import ExternalManager
from mcp_aggregator.federation import load_beacon_id
from mcp_aggregator.oauth import BeaconOAuthManager
from mcp_aggregator.registry import Registry
from mcp_aggregator.web.api import create_web_app

logger = logging.getLogger("mcp_aggregator")


async def discovery_loop(registry: Registry, port: int, interval: float, mcp_url: str | None = None) -> None:
    """Periodically discover MCP servers."""
    while True:
        try:
            responses = await run_discovery(port=port, mcp_url=mcp_url)
            registry.update_from_discovery(responses)
        except Exception as e:
            logger.error("Discovery error: %s", e, exc_info=True)
        await asyncio.sleep(interval)


async def external_loop(manager: ExternalManager, interval: float) -> None:
    """Periodically poll external MCP servers for their tool list."""
    while True:
        try:
            await manager.refresh_all()
        except Exception as e:
            logger.error("External refresh error: %s", e, exc_info=True)
        await asyncio.sleep(interval)


async def main() -> None:
    log_level = os.environ.get("LOG_LEVEL", "info").upper()
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO), format="%(asctime)s %(name)s %(levelname)s %(message)s")

    discovery_port = int(os.environ.get("DISCOVERY_PORT", "9099"))
    discovery_interval = float(os.environ.get("DISCOVERY_INTERVAL", "60"))
    external_interval = float(os.environ.get("EXTERNAL_POLL_INTERVAL", "60"))
    web_port = int(os.environ.get("WEB_PORT", "3000"))
    mcp_url = os.environ.get("MCP_URL", f"http://beacon:{web_port}/mcp")
    public_url = os.environ.get("PUBLIC_URL") or None
    auth_hash = os.environ.get("AUTH_HASH") or None
    oauth_admin_url = os.environ.get("OAUTH_ADMIN_URL") or None

    annotations = AnnotationStore()
    annotations.load()
    logger.info("Loaded %d description override(s)", len(annotations.all()))

    beacon_id = load_beacon_id()
    registry = Registry(annotations=annotations, beacon_id=beacon_id)
    logger.info("Beacon instance id: %s", beacon_id)

    oauth_manager = BeaconOAuthManager()
    logger.info("OAuth redirect URI: %s", oauth_manager.redirect_uri)

    external_manager = ExternalManager(registry, oauth_manager)
    external_manager.load()
    logger.info("Loaded %d external server config(s)", len(external_manager.configs))

    # Initial discovery so locally-discovered servers are available immediately.
    # NOTE: external servers are deliberately NOT polled here. A slow or
    # misbehaving external endpoint (e.g. one that returns no valid MCP response)
    # would otherwise block startup and prevent the web server from ever binding
    # :3000. They are pre-registered as stubs by external_manager.load() above and
    # refreshed in the background by external_loop() in the gather() below.
    logger.info("Running initial discovery...")
    logger.info("Beacon MCP URL: %s", mcp_url)
    responses = await run_discovery(port=discovery_port, mcp_url=mcp_url)
    registry.update_from_discovery(responses)
    logger.info("Found %d discovered server(s)", len(responses))

    web_app = create_web_app(
        registry,
        external_manager,
        annotations,
        oauth_manager,
        discovery_port=discovery_port,
        public_url=public_url,
        auth_hash=auth_hash,
        oauth_admin_url=oauth_admin_url,
    )
    web_config = uvicorn.Config(web_app, host="0.0.0.0", port=web_port, log_level=log_level.lower())
    web_server = uvicorn.Server(web_config)

    logger.info("Starting Beacon on :%d (Web UI + MCP at /mcp)", web_port)

    await asyncio.gather(
        web_server.serve(),
        discovery_loop(registry, discovery_port, discovery_interval, mcp_url=mcp_url),
        external_loop(external_manager, external_interval),
    )


if __name__ == "__main__":
    asyncio.run(main())
