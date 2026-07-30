"""OAuth 2.1 client support for external MCP servers.

The MCP SDK ships the whole protocol (`OAuthClientProvider`: 401 -> protected
resource metadata -> authorization server metadata -> dynamic client
registration -> authorization code + PKCE -> automatic refresh). It is written
for an interactive CLI though: the flow runs *inline* inside the httpx auth
generator, calling `redirect_handler(url)` to open a browser and then blocking
in `callback_handler()` until the code comes back.

Beacon is a daemon, so the code arrives minutes later on a *different* HTTP
request. This module bridges the two:

  - `redirect_handler` stashes the authorization URL and wakes up whoever asked
    to authorize; in non-interactive mode (background polling, proxied tool
    calls) it raises `AuthorizationRequired` immediately instead, so nothing
    ever hangs for five minutes waiting on a human that isn't there.
  - `callback_handler` awaits a future that `GET /api/oauth/callback` resolves.

Tokens and the registered client are persisted per external server so a restart
does not mean re-authorizing.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_client_registration_request,
    create_oauth_metadata_request,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    handle_registration_response,
)
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
)

logger = logging.getLogger(__name__)

DEFAULT_OAUTH_DIR = "/app/data/oauth"

# How long an /authorize API call waits for the provider to hand us the
# authorization URL. This only covers metadata discovery + registration.
AUTHORIZE_URL_TIMEOUT = 45.0

# How long we then wait for the human to finish logging in at the remote IdP.
AUTHORIZE_FLOW_TIMEOUT = 600.0

CALLBACK_PATH = "/api/oauth/callback"


class AuthorizationRequired(Exception):
    """Raised when a flow needs a human but none is present.

    Deliberately an `Exception` (not BaseException) so the existing error
    handling in ExternalManager catches it; callers use `is_auth_required()` to
    find it inside the anyio ExceptionGroups the streamable-HTTP client raises.
    """


def is_auth_required(exc: BaseException) -> bool:
    """True if `exc` (possibly an ExceptionGroup) contains an AuthorizationRequired."""
    from mcp_aggregator.mcp_client import leaf_exceptions

    return any(isinstance(x, AuthorizationRequired) for x in leaf_exceptions(exc))


def ensure_consent_prompt(authorization_url: str) -> str:
    """Add `prompt=consent` when the request asks for offline access.

    OpenID Connect Core 11.1 says an authorization server MUST ignore
    `offline_access` unless the request also carries `prompt=consent`. The MCP
    SDK builds the authorization URL without it, so an OIDC-backed server
    silently drops the scope, issues no refresh token, and the access token dies
    an hour later with no way to renew it — verified against AppShield, where
    adding this one parameter is the difference between getting a refresh token
    and not.

    Only added when offline access is actually requested, so servers that don't
    need it are not pushed through a consent screen on every reconnect.
    """
    parsed = urlparse(authorization_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    scope = " ".join(query.get("scope") or [])
    if "offline_access" not in scope.split() or query.get("prompt"):
        return authorization_url
    query["prompt"] = ["consent"]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _challenge_response(exc: BaseException) -> httpx.Response | None:
    """The 401 response inside a failure, if there is one."""
    from mcp_aggregator.mcp_client import leaf_exceptions

    for leaf in leaf_exceptions(exc):
        if isinstance(leaf, httpx.HTTPStatusError) and leaf.response.status_code == 401:
            return leaf.response
    return None


async def looks_like_oauth(url: str, exc: BaseException) -> bool:
    """Does this failure mean 'authenticate with OAuth' rather than 'broken'?

    Called the first time a server is added, before any provider is attached —
    that is what lets you paste a bare URL and have Beacon work out that it needs
    an authorization flow.
    """
    response = _challenge_response(exc)
    if response is None:
        return False
    # RFC 6750 / RFC 9728: the challenge itself usually says so.
    if "bearer" in response.headers.get("WWW-Authenticate", "").lower():
        return True
    # Bare 401 with no challenge — fall back to looking for the protected
    # resource metadata document at its well-known locations.
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            for candidate in build_protected_resource_metadata_discovery_urls(None, url):
                resp = await client.send(create_oauth_metadata_request(candidate))
                if await handle_protected_resource_response(resp):
                    return True
    except Exception as e:  # pragma: no cover - probing is best effort
        logger.debug("OAuth probe for %s failed: %s", url, e)
    return False


def _oauth_dir() -> Path:
    return Path(os.environ.get("OAUTH_CONFIG_DIR", DEFAULT_OAUTH_DIR))


def default_redirect_base() -> str:
    """Browser-reachable base URL for the OAuth callback.

    Explicit env wins; otherwise derive the origin from PUBLIC_URL (which points
    at /mcp, not the UI root); otherwise assume the published local port.
    """
    explicit = os.environ.get("OAUTH_REDIRECT_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    public = os.environ.get("PUBLIC_URL", "").strip()
    if public:
        parsed = urlparse(public)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return f"http://localhost:{os.environ.get('WEB_PORT', '3000')}"


class FileTokenStorage:
    """SDK `TokenStorage` backed by one JSON file per external server.

    Holds long-lived credentials (refresh tokens), so the file is written 0600.
    """

    def __init__(self, name: str, directory: Path | None = None) -> None:
        self.name = name
        self._dir = directory or _oauth_dir()

    @property
    def path(self) -> Path:
        # Server names come from user config; keep them from escaping the dir.
        safe = self.name.replace("/", "_").replace("\\", "_").replace("..", "_")
        return self._dir / f"{safe}.json"

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to read OAuth state for %r: %s", self.name, e)
            return {}

    def _write(self, data: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # pragma: no cover - best effort on odd filesystems
            logger.warning("Could not chmod 0600 %s", tmp)
        tmp.replace(self.path)

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._read().get("tokens")
        if not raw:
            return None
        try:
            return OAuthToken.model_validate(raw)
        except Exception as e:
            logger.warning("Discarding unreadable tokens for %r: %s", self.name, e)
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        data["obtained_at"] = time.time()
        self._write(data)
        logger.info(
            "Stored OAuth tokens for %r (refresh_token=%s)",
            self.name,
            "yes" if tokens.refresh_token else "no",
        )

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._read().get("client_info")
        if not raw:
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except Exception as e:
            logger.warning("Discarding unreadable client_info for %r: %s", self.name, e)
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)
        logger.info("Registered OAuth client for %r (client_id=%s)", self.name, client_info.client_id)

    # --- non-protocol helpers used by the API/UI ----------------------------

    def has_tokens(self) -> bool:
        return bool(self._read().get("tokens", {}).get("access_token"))

    def get_metadata(self) -> OAuthMetadata | None:
        raw = self._read().get("oauth_metadata")
        if not raw:
            return None
        try:
            return OAuthMetadata.model_validate(raw)
        except Exception as e:
            logger.warning("Discarding unreadable OAuth metadata for %r: %s", self.name, e)
            return None

    def set_metadata(self, metadata: OAuthMetadata) -> None:
        data = self._read()
        data["oauth_metadata"] = metadata.model_dump(mode="json", exclude_none=True)
        self._write(data)

    def expiry_time(self) -> float | None:
        """Absolute expiry of the stored access token, if known."""
        data = self._read()
        tokens = data.get("tokens") or {}
        obtained_at = data.get("obtained_at")
        expires_in = tokens.get("expires_in")
        if not obtained_at or not expires_in:
            return None
        return float(obtained_at) + float(expires_in)

    def summary(self) -> dict:
        data = self._read()
        tokens = data.get("tokens") or {}
        if not tokens:
            return {}
        expires_in = tokens.get("expires_in")
        obtained_at = data.get("obtained_at")
        return {
            "has_refresh_token": bool(tokens.get("refresh_token")),
            "scope": tokens.get("scope"),
            "expires_at": (obtained_at + expires_in) if (obtained_at and expires_in) else None,
        }

    def clear(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as e:  # pragma: no cover
            logger.error("Failed to remove OAuth state for %r: %s", self.name, e)
            return False


class _ScopedOAuthClientProvider(OAuthClientProvider):
    """Provider that pins the requested scope instead of deriving it.

    The SDK implements the MCP spec's scope-selection strategy, which takes the
    scopes from the resource's metadata and overwrites whatever we configured.
    That is spec-correct, but a resource advertising only its own scope (as
    AppShield does: `scopes_supported: ["mcp"]`) leaves no way to ask for
    `offline_access` — so no refresh token is issued and a daemon has to be
    re-authorized by hand every hour.

    Re-apply the configured scope at the last point before the authorization URL
    is built. Only active when the user explicitly configured `scopes`.
    """

    def __init__(self, *args: Any, forced_scope: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._forced_scope = forced_scope

    async def _initialize(self) -> None:
        """Restore the token expiry and the discovered metadata, not just tokens.

        The SDK's `_initialize` loads tokens and client info and stops there,
        which breaks unattended restarts in two ways:

        1. `token_expiry_time` stays None, and `is_token_valid()` reads that as
           "never expires" — so an expired access token is sent anyway and the
           401 it earns pushes the provider into a full re-authorization (needs a
           human) rather than the refresh it was holding a token for.
        2. `oauth_metadata` is empty, so `_get_token_endpoint()` falls back to
           `{origin}/token`. On any server whose real endpoint lives elsewhere
           (AppShield serves `/AppShield/oidc/token`) the refresh POST lands on
           an HTML page and dies parsing it as JSON.

        Restoring both is what lets a restarted Beacon renew silently.
        """
        await super()._initialize()
        storage = self.context.storage
        if not isinstance(storage, FileTokenStorage):  # pragma: no cover
            return
        expiry = storage.expiry_time()
        if expiry is not None:
            self.context.token_expiry_time = expiry
        if self.context.oauth_metadata is None:
            metadata = storage.get_metadata()
            if metadata is not None:
                self.context.oauth_metadata = metadata

    async def _perform_authorization_code_grant(self) -> tuple[str, str]:
        if self._forced_scope:
            self.context.client_metadata.scope = self._forced_scope
        # Discovery has run by this point, so this is the first moment the real
        # endpoints are known. Persist them for the next process (see _initialize).
        storage = self.context.storage
        if isinstance(storage, FileTokenStorage) and self.context.oauth_metadata is not None:
            storage.set_metadata(self.context.oauth_metadata)
        return await super()._perform_authorization_code_grant()


async def register_client(
    server_url: str,
    redirect_uri: str,
    scope: str,
    client_name: str,
) -> OAuthClientInformationFull:
    """Do our own dynamic client registration, with the scope we actually want.

    Pre-seeding storage with the result makes the SDK skip its own registration
    step, so the client ends up registered for the scope we later request.
    """
    metadata = OAuthClientMetadata(
        redirect_uris=[redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name=client_name,
        scope=scope,
    )
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        prm = None
        for url in build_protected_resource_metadata_discovery_urls(None, server_url):
            resp = await client.send(create_oauth_metadata_request(url))
            prm = await handle_protected_resource_response(resp)
            if prm:
                break
        auth_server = str(prm.authorization_servers[0]) if prm and prm.authorization_servers else None

        asm = None
        for url in build_oauth_authorization_server_metadata_discovery_urls(auth_server, server_url):
            resp = await client.send(create_oauth_metadata_request(url))
            ok, candidate = await handle_auth_metadata_response(resp)
            if not ok:
                break
            if candidate:
                asm = candidate
                break

        parsed = urlparse(auth_server or server_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        resp = await client.send(create_client_registration_request(asm, metadata, base))
        return await handle_registration_response(resp)


@dataclass
class _Pending:
    """One in-flight interactive authorization for a single external server."""

    name: str
    url_event: asyncio.Event = field(default_factory=asyncio.Event)
    code_future: asyncio.Future = field(default_factory=asyncio.Future)
    authorize_url: str | None = None
    state: str | None = None
    task: asyncio.Task | None = None
    error: str | None = None


class BeaconOAuthManager:
    """Owns one OAuth provider per external server, plus the interactive bridge."""

    def __init__(self, redirect_base: str | None = None) -> None:
        self.redirect_base = (redirect_base or default_redirect_base()).rstrip("/")
        self._providers: dict[str, OAuthClientProvider] = {}
        self._storages: dict[str, FileTokenStorage] = {}
        self._pending: dict[str, _Pending] = {}
        self._by_state: dict[str, _Pending] = {}
        self._needs_auth: set[str] = set()

    @property
    def redirect_uri(self) -> str:
        return f"{self.redirect_base}{CALLBACK_PATH}"

    def storage_for(self, name: str) -> FileTokenStorage:
        if name not in self._storages:
            self._storages[name] = FileTokenStorage(name)
        return self._storages[name]

    # --- provider construction ---------------------------------------------

    def provider_for(self, cfg) -> OAuthClientProvider:
        """Cached provider for a config.

        One instance per server, shared by polling and by proxied tool calls:
        two providers would each cache their own copy of the tokens and drift
        apart after a refresh.
        """
        provider = self._providers.get(cfg.name)
        if provider is not None:
            return provider

        client_metadata = OAuthClientMetadata(
            redirect_uris=[self.redirect_uri],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            client_name=f"Beacon ({cfg.name})",
            scope=cfg.scopes or None,
        )
        provider = _ScopedOAuthClientProvider(
            server_url=cfg.url,
            client_metadata=client_metadata,
            storage=self.storage_for(cfg.name),
            redirect_handler=self._make_redirect_handler(cfg.name),
            callback_handler=self._make_callback_handler(cfg.name),
            timeout=AUTHORIZE_FLOW_TIMEOUT,
            forced_scope=cfg.scopes or None,
        )
        self._providers[cfg.name] = provider
        return provider

    def forget(self, name: str) -> None:
        """Drop the cached provider so the next use reloads from storage."""
        self._providers.pop(name, None)

    # --- the interactive bridge --------------------------------------------

    def _make_redirect_handler(self, name: str) -> Callable[[str], Awaitable[None]]:
        async def handler(authorization_url: str) -> None:
            pending = self._pending.get(name)
            if pending is None:
                # No human is waiting: a background poll or a proxied tool call
                # hit a 401. Fail now rather than block until the flow times out.
                raise AuthorizationRequired(
                    f"{name}: authorization required — connect it from the Beacon UI"
                )
            authorization_url = ensure_consent_prompt(authorization_url)
            query = parse_qs(urlparse(authorization_url).query)
            state = (query.get("state") or [None])[0]
            pending.authorize_url = authorization_url
            pending.state = state
            if state:
                self._by_state[state] = pending
            pending.url_event.set()

        return handler

    def _make_callback_handler(self, name: str) -> Callable[[], Awaitable[tuple[str, str | None]]]:
        async def handler() -> tuple[str, str | None]:
            pending = self._pending.get(name)
            if pending is None:  # pragma: no cover - redirect_handler ran first
                raise AuthorizationRequired(f"{name}: authorization required")
            try:
                return await asyncio.wait_for(pending.code_future, timeout=AUTHORIZE_FLOW_TIMEOUT)
            except asyncio.TimeoutError:
                raise AuthorizationRequired(f"{name}: authorization timed out")

        return handler

    # --- public API ---------------------------------------------------------

    def status(self, name: str) -> str:
        """One of: authorizing | connected | needs_auth | none."""
        if name in self._pending:
            return "authorizing"
        if self.storage_for(name).has_tokens():
            return "connected"
        if name in self._needs_auth:
            return "needs_auth"
        return "none"

    def summary(self, name: str) -> dict:
        info = {"status": self.status(name)}
        info.update(self.storage_for(name).summary())
        return info

    def mark_needs_auth(self, name: str) -> None:
        self._needs_auth.add(name)

    def clear_needs_auth(self, name: str) -> None:
        self._needs_auth.discard(name)

    async def begin_authorization(
        self,
        cfg,
        on_success: Callable[[], Awaitable[None]] | None = None,
    ) -> str:
        """Start an interactive flow and return the URL the browser must open.

        The flow itself keeps running in the background until the callback comes
        back (or it times out).
        """
        existing = self._pending.get(cfg.name)
        if existing is not None:
            # Already authorizing — hand back the same URL instead of starting a
            # second flow with a different state.
            await asyncio.wait_for(existing.url_event.wait(), timeout=AUTHORIZE_URL_TIMEOUT)
            if existing.authorize_url:
                return existing.authorize_url
            raise RuntimeError(existing.error or "authorization already in progress")

        pending = _Pending(name=cfg.name)
        self._pending[cfg.name] = pending
        pending.task = asyncio.create_task(self._run_flow(cfg, pending, on_success))

        url_wait = asyncio.create_task(pending.url_event.wait())
        done, _ = await asyncio.wait(
            {url_wait, pending.task}, timeout=AUTHORIZE_URL_TIMEOUT, return_when=asyncio.FIRST_COMPLETED
        )
        url_wait.cancel()

        if pending.authorize_url:
            return pending.authorize_url
        if pending.task in done:
            # Finished without ever needing a browser: either it errored, or the
            # stored tokens turned out to be usable after all.
            self._cleanup(pending)
            if pending.error:
                raise RuntimeError(pending.error)
            raise RuntimeError("already authorized")
        self._cleanup(pending)
        raise RuntimeError("timed out waiting for the authorization URL")

    def complete_callback(self, state: str, code: str) -> str | None:
        """Resolve a pending flow from the OAuth redirect. Returns the server name."""
        pending = self._by_state.get(state)
        if pending is None:
            return None
        if not pending.code_future.done():
            pending.code_future.set_result((code, state))
        return pending.name

    def fail_callback(self, state: str, error: str) -> str | None:
        pending = self._by_state.get(state)
        if pending is None:
            return None
        pending.error = error
        if not pending.code_future.done():
            pending.code_future.set_exception(AuthorizationRequired(error))
        return pending.name

    def disconnect(self, name: str) -> bool:
        """Drop stored tokens and the registered client for a server."""
        self.forget(name)
        self._needs_auth.discard(name)
        return self.storage_for(name).clear()

    # --- internals ----------------------------------------------------------

    def _cleanup(self, pending: _Pending) -> None:
        self._pending.pop(pending.name, None)
        if pending.state:
            self._by_state.pop(pending.state, None)

    async def _run_flow(
        self,
        cfg,
        pending: _Pending,
        on_success: Callable[[], Awaitable[None]] | None,
    ) -> None:
        from mcp_aggregator.mcp_client import fetch_remote_tools, format_exc

        try:
            await self._preseed_client_info(cfg)
            # Any authenticated request drives the flow; initialize is the cheapest.
            await fetch_remote_tools(
                cfg.url,
                cfg.headers,
                read_timeout=AUTHORIZE_FLOW_TIMEOUT + 60,
                auth=self.provider_for(cfg),
            )
        except asyncio.CancelledError:
            raise
        except (Exception, BaseExceptionGroup) as e:
            pending.error = format_exc(e)
            logger.error("OAuth authorization for %r failed: %s", cfg.name, pending.error)
            # The provider may hold half-finished state; rebuild it next time.
            self.forget(cfg.name)
            return
        finally:
            self._cleanup(pending)

        logger.info("OAuth authorization for %r completed", cfg.name)
        self.clear_needs_auth(cfg.name)
        if on_success is not None:
            try:
                await on_success()
            except Exception as e:  # pragma: no cover - refresh is best effort
                logger.error("Post-authorization refresh for %r failed: %s", cfg.name, e)

    async def _preseed_client_info(self, cfg) -> None:
        """Register the OAuth client ourselves when the defaults won't do.

        Two cases: the user supplied a pre-registered client_id/secret, or they
        asked for specific scopes (which must be registered, not just requested).
        """
        storage = self.storage_for(cfg.name)
        if await storage.get_client_info():
            return

        client_id = getattr(cfg, "client_id", "") or ""
        if client_id:
            await storage.set_client_info(
                OAuthClientInformationFull(
                    client_id=client_id,
                    client_secret=getattr(cfg, "client_secret", "") or None,
                    redirect_uris=[self.redirect_uri],
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                    scope=cfg.scopes or None,
                )
            )
            return

        if cfg.scopes:
            info = await register_client(
                cfg.url, self.redirect_uri, cfg.scopes, f"Beacon ({cfg.name})"
            )
            await storage.set_client_info(info)
