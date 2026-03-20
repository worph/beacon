"""Mock Tasks MCP server — provides task management tools for testing."""

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
    "mock-tasks",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    stateless_http=True,
)
tasks: dict[str, dict] = {}

MCP_PORT = int(os.environ.get("MCP_PORT", "9099"))
DISCOVERY_PORT = int(os.environ.get("DISCOVERY_PORT", "9099"))


@mcp.tool()
def add_task(title: str, description: str = "") -> dict:
    """Add a new task."""
    task_id = str(uuid.uuid4())[:8]
    tasks[task_id] = {"id": task_id, "title": title, "description": description, "completed": False}
    return tasks[task_id]


@mcp.tool()
def list_tasks() -> list[dict]:
    """List all tasks."""
    return list(tasks.values())


@mcp.tool()
def complete_task(task_id: str) -> dict:
    """Mark a task as completed."""
    if task_id not in tasks:
        return {"error": f"Task {task_id} not found"}
    tasks[task_id]["completed"] = True
    return tasks[task_id]


TOOL_DEFS = [
    {
        "name": "add_task",
        "description": "Add a new task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Task title"},
                "description": {"type": "string", "description": "Task description"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "list_tasks",
        "description": "List all tasks.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "complete_task",
        "description": "Mark a task as completed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task ID"},
            },
            "required": ["task_id"],
        },
    },
]


async def main():
    transport = await create_discovery_responder(
        name="mock-tasks",
        description="A mock task management MCP server",
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
