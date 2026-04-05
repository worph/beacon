# Meta-Tools: Progressive Disclosure

Beacon exposes 4 meta-tools instead of flooding the LLM context with every discovered tool's full schema. The LLM drills down level by level, only paying the context cost for the detail it actually needs.

## Level 0 — Beacon MCP info (automatic on connect)

The LLM receives this just by connecting to Beacon — the `instructions` field plus the tool definitions from `list_tools`. No tool call needed.

**Instructions:**

```
Beacon MCP aggregator. Call server_doc with a server name to get full tool schemas for that server.

Available servers:
- mock-notes — A mock note-taking MCP server
- mock-tasks — A mock task management MCP server
```

**Tool definitions (4 meta-tools + direct tools):**

```json
[
  {"name": "overview",    "description": "List all available tools across discovered MCP servers with names and short descriptions."},
  {"name": "tool_doc",    "description": "Get the full schema and description for a specific tool."},
  {"name": "server_doc",  "description": "Get the full schema and description for all tools on a specific server."},
  {"name": "call",        "description": "Call a tool on a discovered MCP server."},
  {"name": "mock-notes__list_notes", "description": "List all notes."}
]
```

**What you know:** Server names, one-line descriptions, the meta-tool API, and any direct tools. Enough to decide which server to dig into or use direct tools immediately. No tool schemas yet (except direct ones).

## Level 1 — `overview`

Calling `overview` with no arguments returns a compact list of every tool across all servers.

```
## mock-notes
A mock note-taking MCP server
- mock-notes__create_note — Create a new note with a title and content.
- mock-notes__list_notes — List all notes.
- mock-notes__get_note — Get a note by its ID.

## mock-tasks
A mock task management MCP server
- mock-tasks__add_task — Add a new task.
- mock-tasks__list_tasks — List all tasks.
- mock-tasks__complete_task — Mark a task as completed.
```

**What you gain:** The full list of all tool names and one-line descriptions across every server. Enough to decide which tools you need. Still no input schemas.

## Level 2 — `server_doc`

Calling `server_doc` with a server name returns the server description and full schemas for all its tools in one call.

```json
{
  "server": "mock-notes",
  "description": "A mock note-taking MCP server",
  "tools": [
    {
      "name": "mock-notes__create_note",
      "description": "Create a new note with a title and content.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "title": {"type": "string", "description": "Note title"},
          "content": {"type": "string", "description": "Note content"}
        },
        "required": ["title", "content"]
      }
    },
    {
      "name": "mock-notes__list_notes",
      "description": "List all notes.",
      "inputSchema": {"type": "object", "properties": {}}
    },
    {
      "name": "mock-notes__get_note",
      "description": "Get a note by its ID.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "note_id": {"type": "string", "description": "The note ID"}
        },
        "required": ["note_id"]
      }
    }
  ]
}
```

**What you gain:** Full input schemas for every tool on this server in one call. Enough to start calling any tool on this server. You see how tools relate to each other (create, list, get pattern).

## Level 3 — `tool_doc`

Calling `tool_doc` with a namespaced tool name returns the full schema for that single tool.

```json
{
  "name": "mock-notes__create_note",
  "description": "Create a new note with a title and content.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "title": {"type": "string", "description": "Note title"},
      "content": {"type": "string", "description": "Note content"}
    },
    "required": ["title", "content"]
  },
  "server": "mock-notes",
  "server_description": "A mock note-taking MCP server"
}
```

**What you gain:** The same schema detail as `server_doc`, but for one tool only. Useful when you already know exactly which tool you need and don't want the rest.

## Summary

| Level | Call | Context cost | What you learn |
|---|---|---|---|
| 0 | *(automatic)* | ~10 lines | Server names + meta-tool API |
| 1 | `overview` | ~2 lines per tool | All tool names + descriptions |
| 2 | `server_doc` | Full schemas for N tools | Everything to use one server |
| 3 | `tool_doc` | Full schema for 1 tool | Everything to use one tool |

## Typical LLM workflow

Since the instructions already list server names, the most common path is:

1. Read instructions (automatic) — see which servers exist
2. `server_doc("mock-notes")` — get full schemas for the server you need
3. `call("mock-notes__create_note", {"title": "...", "content": "..."})` — use the tools

The `overview` step can be skipped when the server name in the instructions is enough to decide. The `tool_doc` step is there for when you only need one tool and want to minimize context.
