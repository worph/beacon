"""Federation: flatten a remote Beacon's registry into the local one.

A Beacon exposes only its meta-tools (`overview`, `tool_doc`, `server_doc`,
`call`). Registering a remote Beacon as an ordinary external server would
therefore surface `yunderalabs__overview` / `yunderalabs__call` and nothing at
all about the servers behind it, forcing the model through two layers of
indirection and putting the remote schemas out of reach of the local `tool_doc`.

Instead we detect that a remote is a Beacon, pull its registry, and register
each remote server locally as a first-class entry named
`{config}.{remote_server}`. `Registry.resolve_tool` splits on the first `__`,
so the dot in the server segment never collides with tool namespacing.

The transport is MCP, not the remote's REST API: an OAuth-fronted Beacon only
bearer-protects `/mcp`, so the token we hold cannot read `/api/servers`.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

import httpx

from mcp_aggregator.mcp_client import RemoteInfo, call_remote_tool, format_exc
from mcp_aggregator.registry import RegisteredServer

logger = logging.getLogger(__name__)

BEACON_SERVER_NAME = "mcp-aggregator"
META_TOOL_NAMES = {"overview", "tool_doc", "server_doc", "call"}
REGISTRY_TOOL = "beacon_registry"

DEFAULT_BEACON_ID_PATH = "/app/data/beacon-id"

# Lines emitted by Registry.get_instructions(), e.g. "- docmost-mcp — Docmost MCP …"
_INSTRUCTION_LINE = re.compile(r"^-\s+(\S+)\s+—\s*(.*)$")


def load_beacon_id() -> str:
    """Stable per-instance id, created once and persisted next to the other state."""
    path = Path(os.environ.get("BEACON_ID_PATH", DEFAULT_BEACON_ID_PATH))
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except (FileNotFoundError, OSError):
        pass
    generated = uuid.uuid4().hex
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generated)
    except OSError as e:  # pragma: no cover - falls back to an ephemeral id
        logger.warning("Could not persist beacon id at %s: %s", path, e)
    return generated


def is_beacon(info: RemoteInfo) -> bool:
    """True if the remote server is itself a Beacon aggregator."""
    if info.server_name == BEACON_SERVER_NAME:
        return True
    return META_TOOL_NAMES.issubset(info.tool_names())


def _result_text(result) -> str:
    return "".join(
        block.text for block in (result.content or []) if getattr(block, "type", "") == "text"
    )


async def _fetch_via_registry_tool(
    url: str, headers: dict[str, str], auth: httpx.Auth | None
) -> dict | None:
    """Fast path: the remote exposes the structured `beacon_registry` tool.

    Not advertised in the remote's tool list (it is Beacon-to-Beacon plumbing,
    not something an LLM should see), so we simply try it and fall back.
    """
    result = await call_remote_tool(
        url, headers, REGISTRY_TOOL, {}, display_name=REGISTRY_TOOL, auth=auth
    )
    if result.isError:
        logger.debug("Remote %s has no %s tool: %s", url, REGISTRY_TOOL, _result_text(result))
        return None
    try:
        payload = json.loads(_result_text(result))
    except json.JSONDecodeError as e:
        logger.warning("Remote %s returned unparseable %s payload: %s", url, REGISTRY_TOOL, e)
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("servers"), list):
        return None
    return payload


async def _fetch_via_meta_tools(
    url: str, headers: dict[str, str], auth: httpx.Auth | None, info: RemoteInfo
) -> dict:
    """Fallback for a Beacon that predates `beacon_registry`.

    Server names come from the instructions block; the full schemas then come
    from one `server_doc` call per server.
    """
    names: list[tuple[str, str]] = []
    for line in info.instructions.splitlines():
        match = _INSTRUCTION_LINE.match(line.strip())
        if match:
            names.append((match.group(1), match.group(2)))

    async def one(name: str, description: str) -> dict | None:
        result = await call_remote_tool(
            url, headers, "server_doc", {"server_name": name},
            display_name=f"server_doc({name})", auth=auth,
        )
        if result.isError:
            logger.warning("Federation: server_doc(%s) failed on %s", name, url)
            return None
        try:
            doc = json.loads(_result_text(result))
        except json.JSONDecodeError:
            return None
        tools = []
        for tool in doc.get("tools", []):
            # server_doc returns namespaced names; strip back to the bare name.
            bare = tool.get("name", "").split("__", 1)[-1]
            tools.append(
                {
                    "name": bare,
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema", {"type": "object", "properties": {}}),
                }
            )
        return {
            "name": name,
            "description": doc.get("description", description),
            "tools": tools,
            "notes": doc.get("notes"),
            "origin_chain": [],
        }

    docs = await asyncio.gather(*(one(n, d) for n, d in names))
    return {"beacon_id": "", "servers": [d for d in docs if d]}


async def fetch_remote_registry(
    url: str,
    headers: dict[str, str],
    auth: httpx.Auth | None,
    info: RemoteInfo,
) -> dict:
    """Read a remote Beacon's registry, preferring the structured tool."""
    payload = await _fetch_via_registry_tool(url, headers, auth)
    if payload is not None:
        return payload
    return await _fetch_via_meta_tools(url, headers, auth, info)


def build_federated_servers(
    config_name: str,
    url: str,
    headers: dict[str, str],
    payload: dict,
    local_beacon_id: str,
) -> dict[str, RegisteredServer]:
    """Turn a remote registry payload into local registry entries."""
    now = time.time()
    out: dict[str, RegisteredServer] = {}
    for entry in payload.get("servers", []):
        remote_name = entry.get("name")
        if not remote_name:
            continue
        chain = entry.get("origin_chain") or []
        if local_beacon_id and local_beacon_id in chain:
            # We are somewhere upstream of this server already — importing it
            # would close a federation loop.
            logger.info(
                "Skipping %s.%s: federation cycle (this Beacon is already in its origin chain)",
                config_name, remote_name,
            )
            continue
        local_name = f"{config_name}.{remote_name}"
        out[local_name] = RegisteredServer(
            name=local_name,
            description=entry.get("description", "") or "",
            tools=entry.get("tools", []),
            url=url,
            headers=dict(headers),
            origin="external",
            last_seen=now,
            error=None,
            federated_via=config_name,
            remote_name=remote_name,
            origin_chain=list(chain),
        )
    return out
