"""Main entrypoint — runs MCP proxy and web UI concurrently."""

import asyncio
import logging
import os

import uvicorn

from mcp_aggregator.discovery import run_discovery
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


async def main() -> None:
    log_level = os.environ.get("LOG_LEVEL", "info").upper()
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO), format="%(asctime)s %(name)s %(levelname)s %(message)s")

    discovery_port = int(os.environ.get("DISCOVERY_PORT", "9099"))
    discovery_interval = float(os.environ.get("DISCOVERY_INTERVAL", "60"))
    web_port = int(os.environ.get("WEB_PORT", "3000"))
    mcp_url = os.environ.get("MCP_URL", f"http://beacon:{web_port}/mcp")
    public_url = os.environ.get("PUBLIC_URL") or None
    auth_hash = os.environ.get("AUTH_HASH") or None

    registry = Registry()

    # Initial discovery
    logger.info("Running initial discovery...")
    logger.info("Beacon MCP URL: %s", mcp_url)
    responses = await run_discovery(port=discovery_port, mcp_url=mcp_url)
    registry.update_from_discovery(responses)
    logger.info("Found %d server(s)", len(responses))

    web_app = create_web_app(
        registry,
        discovery_port=discovery_port,
        public_url=public_url,
        auth_hash=auth_hash,
    )
    web_config = uvicorn.Config(web_app, host="0.0.0.0", port=web_port, log_level=log_level.lower())
    web_server = uvicorn.Server(web_config)

    logger.info("Starting Beacon on :%d (Web UI + MCP at /mcp)", web_port)

    await asyncio.gather(
        web_server.serve(),
        discovery_loop(registry, discovery_port, discovery_interval, mcp_url=mcp_url),
    )


if __name__ == "__main__":
    asyncio.run(main())
