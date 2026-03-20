"""Main entrypoint — runs MCP proxy and web UI concurrently."""

import asyncio
import logging
import os

import uvicorn

from mcp_aggregator.discovery import run_discovery
from mcp_aggregator.mcp_proxy import create_mcp_app
from mcp_aggregator.registry import Registry
from mcp_aggregator.web.api import create_web_app

logger = logging.getLogger("mcp_aggregator")


async def discovery_loop(registry: Registry, port: int, interval: float) -> None:
    """Periodically discover MCP servers."""
    while True:
        try:
            responses = await run_discovery(port=port)
            registry.update_from_discovery(responses)
        except Exception as e:
            logger.error("Discovery error: %s", e)
        await asyncio.sleep(interval)


async def main() -> None:
    log_level = os.environ.get("LOG_LEVEL", "info").upper()
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO), format="%(asctime)s %(name)s %(levelname)s %(message)s")

    discovery_port = int(os.environ.get("DISCOVERY_PORT", "9099"))
    discovery_interval = float(os.environ.get("DISCOVERY_INTERVAL", "60"))
    mcp_port = int(os.environ.get("MCP_PORT", "9099"))
    web_port = int(os.environ.get("WEB_PORT", "3000"))

    registry = Registry()

    # Initial discovery
    logger.info("Running initial discovery...")
    responses = await run_discovery(port=discovery_port)
    registry.update_from_discovery(responses)
    logger.info("Found %d server(s)", len(responses))

    mcp_app = create_mcp_app(registry)
    web_app = create_web_app(registry, discovery_port=discovery_port)

    mcp_config = uvicorn.Config(mcp_app, host="0.0.0.0", port=mcp_port, log_level=log_level.lower())
    web_config = uvicorn.Config(web_app, host="0.0.0.0", port=web_port, log_level=log_level.lower())

    mcp_server = uvicorn.Server(mcp_config)
    web_server = uvicorn.Server(web_config)

    logger.info("Starting MCP endpoint on :%d/mcp", mcp_port)
    logger.info("Starting Web UI on :%d", web_port)

    await asyncio.gather(
        mcp_server.serve(),
        web_server.serve(),
        discovery_loop(registry, discovery_port, discovery_interval),
    )


if __name__ == "__main__":
    asyncio.run(main())
