"""Mock Notes MCP server — provides note-taking tools for testing."""

import asyncio
import logging
import os
import uuid

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp_announce import create_discovery_responder

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

mcp = FastMCP(
    "mock-notes",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    stateless_http=True,
)
notes: dict[str, dict] = {}

MCP_PORT = int(os.environ.get("MCP_PORT", "9099"))
DISCOVERY_PORT = int(os.environ.get("DISCOVERY_PORT", "9099"))


@mcp.tool()
def create_note(title: str, content: str) -> dict:
    """Create a new note with a title and content."""
    note_id = str(uuid.uuid4())[:8]
    notes[note_id] = {"id": note_id, "title": title, "content": content}
    return notes[note_id]


@mcp.tool()
def list_notes() -> list[dict]:
    """List all notes."""
    return list(notes.values())


@mcp.tool()
def get_note(note_id: str) -> dict:
    """Get a note by its ID."""
    if note_id not in notes:
        return {"error": f"Note {note_id} not found"}
    return notes[note_id]


TOOL_DEFS = [
    {
        "name": "create_note",
        "description": "Create a new note with a title and content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Note title"},
                "content": {"type": "string", "description": "Note content"},
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "list_notes",
        "description": "List all notes.",
        "inputSchema": {"type": "object", "properties": {}},
        "direct": True,
    },
    {
        "name": "get_note",
        "description": "Get a note by its ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "note_id": {"type": "string", "description": "The note ID"},
            },
            "required": ["note_id"],
        },
    },
]


async def main():
    transport = await create_discovery_responder(
        name="mock-notes",
        description="A mock note-taking MCP server",
        tools=TOOL_DEFS,
        port=MCP_PORT,
        listen_port=DISCOVERY_PORT,
    )

    app = mcp.streamable_http_app()
    config = uvicorn.Config(app, host="0.0.0.0", port=MCP_PORT, log_level="info")
    server = uvicorn.Server(config)

    try:
        await server.serve()
    finally:
        transport.close()


if __name__ == "__main__":
    asyncio.run(main())
