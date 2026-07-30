"""External (non-Beacon-ready) MCP servers: HTTP URLs, persisted to disk.

Two ways to authenticate:

  - static `headers` (a bearer token you already hold), and
  - OAuth 2.1, where Beacon runs the full authorization-code flow against the
    remote and stores the resulting tokens (see `oauth.py`).

A remote that turns out to be a Beacon itself is *federated* rather than nested:
its servers are imported as first-class local entries (see `federation.py`).
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
import json
import os

from mcp_aggregator.federation import build_federated_servers, fetch_remote_registry, is_beacon
from mcp_aggregator.mcp_client import fetch_remote_tools, format_exc
from mcp_aggregator.oauth import BeaconOAuthManager, is_auth_required, looks_like_oauth
from mcp_aggregator.registry import RegisteredServer, Registry

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "/app/data/external.json"


@dataclass
class ExternalConfig:
    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    description: str = ""
    # OAuth. `oauth` is set once a 401 proves the remote wants it (or by the
    # user up front). `scopes` overrides the scopes Beacon requests — needed
    # when a resource under-advertises and would otherwise never hand out a
    # refresh token. client_id/client_secret cover servers without open DCR.
    oauth: bool = False
    scopes: str = ""
    client_id: str = ""
    client_secret: str = ""
    # "auto" federates when the remote is detected to be a Beacon.
    federate: str = "auto"  # "auto" | "on" | "off"

    def to_json(self) -> dict:
        entry: dict = {"type": "http", "url": self.url}
        if self.headers:
            entry["headers"] = self.headers
        if self.description:
            entry["description"] = self.description
        if self.oauth:
            entry["oauth"] = True
        if self.scopes:
            entry["scopes"] = self.scopes
        if self.client_id:
            entry["client_id"] = self.client_id
        if self.client_secret:
            entry["client_secret"] = self.client_secret
        if self.federate != "auto":
            entry["federate"] = self.federate
        return entry

    def wants_federation(self, info) -> bool:
        if self.federate == "off":
            return False
        if self.federate == "on":
            return True
        return is_beacon(info)


def _config_path() -> Path:
    return Path(os.environ.get("EXTERNAL_CONFIG_PATH", DEFAULT_CONFIG_PATH))


def config_from_entry(name: str, entry: dict) -> ExternalConfig:
    """Build a config from one `mcpServers`-style entry. Raises ValueError."""
    url = entry.get("url")
    if not url:
        raise ValueError(f"{name}: missing url")
    headers = entry.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValueError(f"{name}: headers must be an object")
    federate = str(entry.get("federate", "auto") or "auto")
    if federate not in ("auto", "on", "off"):
        raise ValueError(f"{name}: federate must be one of auto, on, off")
    return ExternalConfig(
        name=name,
        url=str(url),
        headers={str(k): str(v) for k, v in headers.items()},
        description=str(entry.get("description", "") or ""),
        oauth=bool(entry.get("oauth", False)),
        scopes=str(entry.get("scopes", "") or ""),
        client_id=str(entry.get("client_id", "") or ""),
        client_secret=str(entry.get("client_secret", "") or ""),
        federate=federate,
    )


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
        try:
            out[name] = config_from_entry(name, entry)
        except ValueError as e:
            logger.warning("Skipping external server %r: %s", name, e)
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

    def __init__(self, registry: Registry, oauth: BeaconOAuthManager | None = None) -> None:
        self.registry = registry
        self.oauth = oauth or BeaconOAuthManager()
        self.configs: dict[str, ExternalConfig] = {}

    def load(self) -> None:
        self.configs = load_configs()
        # Pre-register so tools appear even before the first poll completes.
        for cfg in self.configs.values():
            self._register_stub(cfg)

    def auth_for(self, cfg: ExternalConfig):
        """The httpx auth flow for a config, or None when it needs no OAuth."""
        if not cfg.oauth:
            return None
        return self.oauth.provider_for(cfg)

    def auth_for_server(self, srv: RegisteredServer):
        """Resolve a registry entry back to its config's auth flow.

        Federated entries carry the owning config in `federated_via`; a plain
        external server is named after its config. Discovered servers have no
        config and get None.
        """
        cfg = self.configs.get(srv.federated_via or srv.name)
        return self.auth_for(cfg) if cfg else None

    def _previous(self, cfg: ExternalConfig) -> RegisteredServer | None:
        """The single non-federated entry for a config, if there is one."""
        for srv in self.registry.external_group(cfg.name):
            if srv.name == cfg.name:
                return srv
        return None

    def _register_stub(self, cfg: ExternalConfig) -> None:
        existing = self._previous(cfg)
        if self.registry.external_group(cfg.name) and existing is None:
            # Already federated into several entries — leave them in place until
            # the next poll rather than collapsing back to a stub.
            return
        self.registry.replace_external_group(
            cfg.name,
            {
                cfg.name: RegisteredServer(
                    name=cfg.name,
                    description=cfg.description or (existing.description if existing else ""),
                    tools=existing.tools if existing else [],
                    url=cfg.url,
                    headers=dict(cfg.headers),
                    origin="external",
                    last_seen=existing.last_seen if existing else time.time(),
                    error=existing.error if existing else None,
                    auth_required=existing.auth_required if existing else False,
                )
            },
        )

    def list_configs(self) -> list[ExternalConfig]:
        return list(self.configs.values())

    def get(self, name: str) -> ExternalConfig | None:
        return self.configs.get(name)

    def upsert(self, cfg: ExternalConfig) -> None:
        self.configs[cfg.name] = cfg
        # The URL or scopes may have changed; the cached provider is bound to
        # the old ones.
        self.oauth.forget(cfg.name)
        self._register_stub(cfg)
        save_configs(self.configs)

    def remove(self, name: str) -> bool:
        removed = self.configs.pop(name, None) is not None
        if removed:
            self.registry.remove_external_group(name)
            self.oauth.disconnect(name)
            save_configs(self.configs)
        return removed

    def _set_error(self, cfg: ExternalConfig, message: str, auth_required: bool) -> None:
        """Record a failure without discarding tools we already know about."""
        group = {s.name: s for s in self.registry.external_group(cfg.name)}
        if not group:
            group = {
                cfg.name: RegisteredServer(
                    name=cfg.name,
                    description=cfg.description,
                    tools=[],
                    url=cfg.url,
                    headers=dict(cfg.headers),
                    origin="external",
                )
            }
        for srv in group.values():
            srv.error = message
            srv.auth_required = auth_required
        self.registry.replace_external_group(cfg.name, group)

    async def refresh_one(self, cfg: ExternalConfig) -> None:
        # Don't even open a connection for a server we know needs a human: the
        # provider would raise on the redirect anyway, and this keeps the poll
        # loop quiet.
        if cfg.oauth and self.oauth.status(cfg.name) in ("needs_auth", "authorizing"):
            self._set_error(cfg, "Authorization required", auth_required=True)
            return

        auth = self.auth_for(cfg)
        try:
            info = await fetch_remote_tools(cfg.url, cfg.headers, auth=auth)
        except BaseException as e:
            if not isinstance(e, (Exception, BaseExceptionGroup)):
                raise
            # Either the provider already told us a human is needed, or this is
            # first contact with a server we didn't know speaks OAuth.
            if is_auth_required(e) or (not cfg.oauth and await looks_like_oauth(cfg.url, e)):
                logger.info("External server %r needs authorization", cfg.name)
                self.oauth.mark_needs_auth(cfg.name)
                if not cfg.oauth:
                    cfg.oauth = True
                    self.configs[cfg.name] = cfg
                    self.oauth.forget(cfg.name)
                    save_configs(self.configs)
                self._set_error(cfg, "Authorization required", auth_required=True)
                return
            msg = format_exc(e)
            logger.error("External server %r poll failed: %s", cfg.name, msg)
            self._set_error(cfg, msg, auth_required=False)
            return

        self.oauth.clear_needs_auth(cfg.name)

        if cfg.wants_federation(info):
            await self._refresh_federated(cfg, info, auth)
            return

        self.registry.replace_external_group(
            cfg.name,
            {
                cfg.name: RegisteredServer(
                    name=cfg.name,
                    description=cfg.description or info.instructions,
                    tools=info.tools,
                    url=cfg.url,
                    headers=dict(cfg.headers),
                    origin="external",
                    last_seen=time.time(),
                    error=None,
                )
            },
        )

    async def _refresh_federated(self, cfg: ExternalConfig, info, auth) -> None:
        try:
            payload = await fetch_remote_registry(cfg.url, cfg.headers, auth, info)
        except BaseException as e:
            if not isinstance(e, (Exception, BaseExceptionGroup)):
                raise
            msg = format_exc(e)
            logger.error("Federation of %r failed: %s", cfg.name, msg)
            self._set_error(cfg, msg, auth_required=is_auth_required(e))
            return

        servers = build_federated_servers(
            cfg.name, cfg.url, cfg.headers, payload, self.registry.beacon_id
        )
        if not servers:
            # A Beacon with nothing behind it — keep a single entry so the user
            # can still see it is connected.
            servers = {
                cfg.name: RegisteredServer(
                    name=cfg.name,
                    description=cfg.description or info.instructions,
                    tools=[],
                    url=cfg.url,
                    headers=dict(cfg.headers),
                    origin="external",
                    last_seen=time.time(),
                )
            }
        self.registry.replace_external_group(cfg.name, servers)
        logger.info("Federated %d server(s) from %r", len(servers), cfg.name)

    async def refresh_all(self) -> None:
        if not self.configs:
            return
        await asyncio.gather(*(self.refresh_one(cfg) for cfg in self.configs.values()))

    async def authorize(self, cfg: ExternalConfig) -> str:
        """Start an interactive OAuth flow; returns the URL for the browser."""
        if not cfg.oauth:
            cfg.oauth = True
            self.configs[cfg.name] = cfg
            save_configs(self.configs)

        async def on_success() -> None:
            await self.refresh_one(cfg)

        return await self.oauth.begin_authorization(cfg, on_success=on_success)
