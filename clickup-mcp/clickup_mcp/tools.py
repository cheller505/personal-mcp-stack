"""MCP tool definitions and handlers."""

import asyncio
import json
import logging
import re
from typing import Callable

from mcp.types import TextContent, Tool

from . import database as db
from . import sync as sync_module
from .api import ClickUpClient

logger = logging.getLogger(__name__)


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="list_workspaces",
            description="List ClickUp workspaces (teams) accessible to the configured token.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="list_spaces",
            description="List spaces, optionally filtered to one workspace.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string", "description": "Limit to this workspace ID"},
                },
                "required": [],
            },
        ),
        Tool(
            name="list_folders",
            description="List folders, optionally filtered to one space.",
            inputSchema={
                "type": "object",
                "properties": {
                    "space_id": {"type": "string", "description": "Limit to this space ID"},
                },
                "required": [],
            },
        ),
        Tool(
            name="list_lists",
            description="List task lists, optionally filtered by folder or space.",
            inputSchema={
                "type": "object",
                "properties": {
                    "folder_id": {"type": "string"},
                    "space_id": {"type": "string"},
                },
                "required": [],
            },
        ),
        Tool(
            name="search_tasks",
            description=(
                "Full-text search across task name, description, and tags. "
                "Supports inline filter tokens in the query: "
                "assignee:foo status:open priority:1 list:\"My List\" tag:bug "
                "due:2025-12-01..2025-12-31"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query with optional inline filters"},
                    "include_closed": {"type": "boolean", "default": True},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_tasks",
            description="List tasks in a given list, newest-updated first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "list_id": {"type": "string"},
                    "include_closed": {"type": "boolean", "default": True},
                    "limit": {"type": "integer", "default": 100},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": ["list_id"],
            },
        ),
        Tool(
            name="get_task",
            description="Retrieve full details for a single task by ID.",
            inputSchema={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        ),
        Tool(
            name="get_subtasks",
            description="List subtasks of a parent task.",
            inputSchema={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        ),
        Tool(
            name="get_task_comments",
            description="Get comments on a task (cached; fetches from API if not yet cached).",
            inputSchema={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        ),
        Tool(
            name="create_task",
            description=(
                "Create a new task in the given list. Accepts ClickUp native fields "
                "(name, description, status, priority [1=urgent..4=low], assignees, "
                "tags, due_date, start_date, time_estimate, parent)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "list_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "status": {"type": "string"},
                    "priority": {"type": "integer", "description": "1=urgent, 2=high, 3=normal, 4=low"},
                    "assignees": {"type": "array", "items": {"type": "integer"}},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "due_date": {"type": "integer", "description": "Unix ms"},
                    "start_date": {"type": "integer", "description": "Unix ms"},
                    "time_estimate": {"type": "integer", "description": "ms"},
                    "parent": {"type": "string", "description": "Parent task ID (creates a subtask)"},
                    "notify_all": {"type": "boolean", "default": False},
                },
                "required": ["list_id", "name"],
            },
        ),
        Tool(
            name="update_task",
            description="Update fields on an existing task. Same field names as create_task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "status": {"type": "string"},
                    "priority": {"type": "integer"},
                    "assignees_add": {"type": "array", "items": {"type": "integer"}},
                    "assignees_rem": {"type": "array", "items": {"type": "integer"}},
                    "due_date": {"type": "integer"},
                    "start_date": {"type": "integer"},
                    "time_estimate": {"type": "integer"},
                    "archived": {"type": "boolean"},
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="add_comment",
            description="Post a comment on a task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "comment_text": {"type": "string"},
                    "notify_all": {"type": "boolean", "default": False},
                },
                "required": ["task_id", "comment_text"],
            },
        ),
        Tool(
            name="get_member",
            description="Look up a workspace member by ID, username, or email.",
            inputSchema={
                "type": "object",
                "properties": {
                    "member_id": {"type": "string"},
                    "username": {"type": "string"},
                    "email": {"type": "string"},
                },
                "required": [],
            },
        ),
        Tool(
            name="sync_status",
            description="Return sync metadata: last sync times, total task count, errors.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="force_sync",
            description="Trigger an immediate delta sync in the background.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


def _text(content: str) -> list[TextContent]:
    return [TextContent(type="text", text=content)]


async def handle_tool(name: str, arguments: dict, get_token: Callable[[], str]) -> list[TextContent]:
    try:
        match name:
            case "list_workspaces":
                return _list_workspaces()
            case "list_spaces":
                return _list_spaces(arguments)
            case "list_folders":
                return _list_folders(arguments)
            case "list_lists":
                return _list_lists(arguments)
            case "search_tasks":
                return _search_tasks(arguments)
            case "get_tasks":
                return _get_tasks(arguments)
            case "get_task":
                return _get_task(arguments)
            case "get_subtasks":
                return _get_subtasks(arguments)
            case "get_task_comments":
                return await _get_task_comments(arguments, get_token)
            case "create_task":
                return await _create_task(arguments, get_token)
            case "update_task":
                return await _update_task(arguments, get_token)
            case "add_comment":
                return await _add_comment(arguments, get_token)
            case "get_member":
                return _get_member(arguments)
            case "sync_status":
                return _sync_status()
            case "force_sync":
                return await _force_sync(get_token)
            case _:
                return _text(f"Unknown tool: {name}")
    except Exception as exc:
        logger.error("Tool %s error: %s", name, exc, exc_info=True)
        return _text(f"Error executing {name}: {exc}")


# ── Filter token parsing ─────────────────────────────────────────────────────

_FILTER_KEYS = ("assignee", "status", "priority", "list", "tag", "due")

# Match key:value where value is either "quoted text" or a bare run of non-space chars
_TOKEN_RE = re.compile(
    r'\b(' + '|'.join(_FILTER_KEYS) + r')'
    r':(?:"([^"]*)"|(\S+))',
    re.IGNORECASE,
)


def _parse_filters(query: str) -> tuple[str, dict]:
    filters: dict = {}
    def _replace(m: re.Match) -> str:
        key = m.group(1).lower()
        value = m.group(2) if m.group(2) is not None else m.group(3)
        filters[key] = value
        return ""

    stripped = _TOKEN_RE.sub(_replace, query).strip()
    stripped = re.sub(r'\s+', ' ', stripped)
    return stripped, filters


def _resolve_list_id(name_or_id: str) -> str | None:
    """Accept either a list ID or a name."""
    if not name_or_id:
        return None
    with db.get_conn() as conn:
        row = conn.execute("SELECT id FROM lists WHERE id = ?", (name_or_id,)).fetchone()
        if row:
            return row["id"]
        row = conn.execute(
            "SELECT id FROM lists WHERE lower(name) = lower(?) LIMIT 1", (name_or_id,)
        ).fetchone()
        return row["id"] if row else None


# ── Tool impls ───────────────────────────────────────────────────────────────

def _list_workspaces() -> list[TextContent]:
    rows = db.get_workspaces()
    if not rows:
        return _text("No workspaces cached yet. Run a sync first.")
    lines = []
    for r in rows:
        lines.append(f"• {r['name']}  (id: {r['id']})")
    return _text("\n".join(lines))


def _list_spaces(args: dict) -> list[TextContent]:
    rows = db.get_spaces(args.get("workspace_id"))
    if not rows:
        return _text("No spaces found.")
    lines = []
    for r in rows:
        tag = " [archived]" if r["archived"] else ""
        lines.append(f"• {r['name']}{tag}  (id: {r['id']}, workspace: {r['workspace_id']})")
    return _text("\n".join(lines))


def _list_folders(args: dict) -> list[TextContent]:
    rows = db.get_folders(args.get("space_id"))
    if not rows:
        return _text("No folders found.")
    lines = []
    for r in rows:
        tag = " [archived]" if r["archived"] else ""
        lines.append(f"• {r['name']}{tag}  (id: {r['id']}, space: {r['space_id']})")
    return _text("\n".join(lines))


def _list_lists(args: dict) -> list[TextContent]:
    rows = db.get_lists(args.get("folder_id"), args.get("space_id"))
    if not rows:
        return _text("No lists found.")
    lines = []
    for r in rows:
        tag = " [archived]" if r["archived"] else ""
        sync_tag = "" if r["fully_synced"] else "  [not synced]"
        lines.append(
            f"• {r['name']}{tag}  ({r['task_count']} tasks){sync_tag}\n"
            f"  ID: {r['id']}  folder: {r['folder_id']}  space: {r['space_id']}"
        )
    return _text("\n".join(lines))


def _search_tasks(args: dict) -> list[TextContent]:
    raw_query = args.get("query", "")
    text, filters = _parse_filters(raw_query)

    list_id: str | None = None
    if "list" in filters:
        list_id = _resolve_list_id(filters["list"])
        if not list_id:
            return _text(f"List not found: {filters['list']}")

    priority: int | None = None
    if "priority" in filters:
        try:
            priority = int(filters["priority"])
        except ValueError:
            return _text(f"Invalid priority: {filters['priority']}")

    due_from = due_to = None
    if "due" in filters:
        val = filters["due"]
        if ".." in val:
            a, b = val.split("..", 1)
            due_from = a or None
            due_to = b or None
        else:
            due_from = due_to = val

    results = db.search_tasks_sql(
        fts_query=text or None,
        list_id=list_id,
        status=filters.get("status"),
        priority=priority,
        assignee=filters.get("assignee"),
        tag=filters.get("tag"),
        due_from=due_from,
        due_to=due_to,
        include_closed=bool(args.get("include_closed", True)),
        limit=min(int(args.get("limit", 50)), 200),
    )

    if not results:
        return _text("No tasks matched.")

    lines = [f"Found {len(results)} task(s):\n"]
    for t in results:
        assignees = json.loads(t.get("assignees") or "[]")
        a_str = ", ".join(a.get("username", "") for a in assignees) or "—"
        lines.append(
            f"[{t.get('status') or '?'}] {t.get('name')}\n"
            f"  list: {t.get('list_name')}  assignees: {a_str}  priority: {t.get('priority')}\n"
            f"  id: {t.get('task_id')}  updated: {t.get('date_updated')}\n"
            f"  url: {t.get('url')}\n"
        )
    return _text("\n".join(lines))


def _get_tasks(args: dict) -> list[TextContent]:
    list_id = args["list_id"]
    include_closed = bool(args.get("include_closed", True))
    limit = min(int(args.get("limit", 100)), 500)
    offset = int(args.get("offset", 0))
    tasks = db.get_tasks_by_list(list_id, include_closed, limit, offset)
    if not tasks:
        return _text("No tasks found.")
    lines = []
    for t in tasks:
        assignees = json.loads(t.get("assignees") or "[]")
        a_str = ", ".join(a.get("username", "") for a in assignees) or "—"
        lines.append(
            f"[{t.get('status') or '?'}] {t.get('name')}  ({a_str})\n"
            f"  id: {t.get('task_id')}  priority: {t.get('priority')}  due: {t.get('due_date')}"
        )
    return _text("\n".join(lines))


def _get_task(args: dict) -> list[TextContent]:
    t = db.get_task_by_id(args["task_id"])
    if not t:
        return _text("Task not found in local cache.")
    assignees = json.loads(t.get("assignees") or "[]")
    a_str = ", ".join(f"{a.get('username','')} <{a.get('email','')}>" for a in assignees) or "—"
    tags = ", ".join(json.loads(t.get("tags") or "[]")) or "—"
    return _text(
        f"Task: {t['name']}\n"
        f"Status: {t['status']}  Priority: {t['priority']}\n"
        f"List: {t['list_name']}  Folder: {t['folder_name']}  Space: {t['space_name']}\n"
        f"Assignees: {a_str}\n"
        f"Tags: {tags}\n"
        f"Created: {t['date_created']}  Updated: {t['date_updated']}  Closed: {t['date_closed']}\n"
        f"Due: {t['due_date']}  Start: {t['start_date']}\n"
        f"Time estimate: {t['time_estimate']}  Time spent: {t['time_spent']}\n"
        f"Creator: {t['creator_name']}\n"
        f"Parent: {t['parent_task_id']}\n"
        f"URL: {t['url']}\n"
        f"ID: {t['task_id']}\n"
        f"\n{'─' * 60}\n\n"
        f"{t.get('description') or '(no description)'}"
    )


def _get_subtasks(args: dict) -> list[TextContent]:
    subs = db.get_subtasks(args["task_id"])
    if not subs:
        return _text("No subtasks.")
    lines = []
    for s in subs:
        lines.append(f"[{s.get('status')}] {s.get('name')}  (id: {s['task_id']})")
    return _text("\n".join(lines))


async def _get_task_comments(args: dict, get_token: Callable[[], str]) -> list[TextContent]:
    task_id = args["task_id"]
    cached = db.get_comments_for_task(task_id)
    if not cached:
        async with ClickUpClient(get_token) as client:
            raw = await client.get_task_comments(task_id)
        with db.get_conn() as conn:
            for c in raw:
                db.upsert_comment(conn, _norm_comment(c, task_id))
        cached = db.get_comments_for_task(task_id)

    if not cached:
        return _text("No comments.")
    lines = []
    for c in cached:
        lines.append(
            f"[{c.get('date')}] user={c.get('user_id')} resolved={bool(c.get('resolved'))}\n"
            f"  {c.get('comment_text')}\n"
        )
    return _text("\n".join(lines))


def _norm_comment(c: dict, task_id: str) -> dict:
    text_parts = []
    for chunk in c.get("comment", []) or []:
        if isinstance(chunk, dict):
            text_parts.append(chunk.get("text", ""))
    text = "".join(text_parts) or c.get("comment_text", "")
    user = c.get("user") or {}
    return {
        "id": str(c.get("id", "")),
        "task_id": task_id,
        "comment_text": text,
        "user_id": str(user.get("id")) if user.get("id") else None,
        "date": c.get("date", ""),
        "resolved": 1 if c.get("resolved") else 0,
    }


def _build_task_payload(args: dict, *, for_create: bool) -> dict:
    payload: dict = {}
    direct = ("name", "description", "status", "priority", "assignees", "tags",
              "due_date", "start_date", "time_estimate", "parent", "archived")
    for k in direct:
        if k in args and args[k] is not None:
            payload[k] = args[k]

    # update_task may use assignees_add / assignees_rem
    if not for_create:
        if "assignees_add" in args or "assignees_rem" in args:
            payload["assignees"] = {
                "add": args.get("assignees_add", []) or [],
                "rem": args.get("assignees_rem", []) or [],
            }
    return payload


async def _create_task(args: dict, get_token: Callable[[], str]) -> list[TextContent]:
    list_id = args["list_id"]
    payload = _build_task_payload(args, for_create=True)
    payload.pop("archived", None)
    async with ClickUpClient(get_token) as client:
        result = await client.create_task(list_id, payload)
    return _text(f"Task created. ID: {result.get('id')}  URL: {result.get('url')}")


async def _update_task(args: dict, get_token: Callable[[], str]) -> list[TextContent]:
    task_id = args["task_id"]
    payload = _build_task_payload(args, for_create=False)
    if not payload:
        return _text("No fields supplied to update.")
    async with ClickUpClient(get_token) as client:
        result = await client.update_task(task_id, payload)
    return _text(f"Task {task_id} updated.  URL: {result.get('url')}")


async def _add_comment(args: dict, get_token: Callable[[], str]) -> list[TextContent]:
    async with ClickUpClient(get_token) as client:
        result = await client.add_comment(
            args["task_id"], args["comment_text"], bool(args.get("notify_all", False))
        )
    return _text(f"Comment added. ID: {result.get('id')}")


def _get_member(args: dict) -> list[TextContent]:
    m = db.get_member(args.get("member_id"), args.get("username"), args.get("email"))
    if not m:
        return _text("Member not found.")
    return _text(
        f"Username: {m.get('username')}\n"
        f"Email:    {m.get('email')}\n"
        f"ID:       {m.get('id')}\n"
        f"Color:    {m.get('color')}"
    )


def _sync_status() -> list[TextContent]:
    s = sync_module.get_sync_status()
    return _text(
        f"Last full sync:  {s['last_full_sync'] or 'Never'}\n"
        f"Last delta sync: {s['last_delta_sync'] or 'Never'}\n"
        f"Total tasks:     {s['total_tasks']:,}\n"
        f"Sync running:    {'Yes' if s['sync_in_progress'] else 'No'}\n"
        f"Last error:      {s['last_error'] or 'None'}"
    )


async def _force_sync(get_token: Callable[[], str]) -> list[TextContent]:
    asyncio.create_task(sync_module.run_delta_sync(get_token))
    return _text("Delta sync triggered in the background. Check sync_status for progress.")
