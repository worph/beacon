"""External (non-Beacon-ready) MCP servers: HTTP URLs with optional headers, persisted to disk."""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from mcp_aggregator.mcp_client import fetch_remote_tools, format_exc
from mcp_aggregator.registry import RegisteredServer, Registry

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "/app/data/external.json"


@dataclass
class ExternalConfig:
    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    description: str = ""

    def to_json(self) -> dict:
        entry: dict = {"type": "http", "url": self.url}
        if self.headers:
            entry["headers"] = self.headers
        if self.description:
            entry["description"] = self.description
        return entry


def _config_path() -> Path:
    return Path(os.environ.get("EXTERNAL_CONFIG_PATH", DEFAULT_CONFIG_PATH))


def load_configs() -> dict[str, ExternalConfig]:
    """Load external server configs from disk.

    Accepts both our own format and the Claude Desktop `mcpServers` shape.
    """
    path = _config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to load external config at %s: %s", path, e)
        return {}
    raw = data.get("mcpServers") if isinstance(data, dict) and "mcpServers" in data else data
    if not isinstance(raw, dict):
        return {}
    out: dict[str, ExternalConfig] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if not url:
            logger.warning("Skipping external server %r: missing url", name)
            continue
        headers = entry.get("headers") or {}
        if not isinstance(headers, dict):
            logger.warning("Skipping external server %r: headers must be an object", name)
            continue
        out[name] = ExternalConfig(
            name=name,
            url=url,
            headers={str(k): str(v) for k, v in headers.items()},
            description=entry.get("description", "") or "",
        )
    return out


def save_configs(configs: dict[str, ExternalConfig]) -> None:
    """Persist external server configs to disk atomically."""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"mcpServers": {name: c.to_json() for name, c in configs.items()}}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


class ExternalManager:
    """Manages external MCP servers: persistence + periodic tool-list refresh."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.configs: dict[str, ExternalConfig] = {}

    def load(self) -> None:
        self.configs = load_configs()
        # Pre-register so tools appear even before the first poll completes.
        for cfg in self.configs.values():
            self._register_stub(cfg)

    def _register_stub(self, cfg: ExternalConfig) -> None:
        existing = self.registry._external.get(cfg.name)  # noqa: SLF001
        self.registry.set_external(
            cfg.name,
            RegisteredServer(
                name=cfg.name,
                description=cfg.description or (existing.description if existing else ""),
                tools=existing.tools if existing else [],
                url=cfg.url,
                headers=dict(cfg.headers),
                origin="external",
                last_seen=existing.last_seen if existing else time.time(),
                error=existing.error if existing else None,
            ),
        )

    def list_configs(self) -> list[ExternalConfig]:
        return list(self.configs.values())

    def upsert(self, cfg: ExternalConfig) -> None:
        self.configs[cfg.name] = cfg
        self._register_stub(cfg)
        save_configs(self.configs)

    def remove(self, name: str) -> bool:
        removed = self.configs.pop(name, None) is not None
        if removed:
            self.registry.remove_external(name)
            save_configs(self.configs)
        return removed

    async def refresh_one(self, cfg: ExternalConfig) -> None:
        try:
            description, tools = await fetch_remote_tools(cfg.url, cfg.headers)
        except BaseException as e:
            if not isinstance(e, (Exception, BaseExceptionGroup)):
                raise
            msg = format_exc(e)
            logger.error("External server %r poll failed: %s", cfg.name, msg)
            existing = self.registry._external.get(cfg.name)  # noqa: SLF001
            self.registry.set_external(
                cfg.name,
                RegisteredServer(
                    name=cfg.name,
                    description=cfg.description or (existing.description if existing else ""),
                    tools=existing.tools if existing else [],
                    url=cfg.url,
                    headers=dict(cfg.headers),
                    origin="external",
                    last_seen=existing.last_seen if existing else time.time(),
                    error=msg,
                ),
            )
            return
        self.registry.set_external(
            cfg.name,
            RegisteredServer(
                name=cfg.name,
                description=cfg.description or description,
                tools=tools,
                url=cfg.url,
                headers=dict(cfg.headers),
                origin="external",
                last_seen=time.time(),
                error=None,
            ),
        )

    async def refresh_all(self) -> None:
        if not self.configs:
            return
        await asyncio.gather(*(self.refresh_one(cfg) for cfg in self.configs.values()))
