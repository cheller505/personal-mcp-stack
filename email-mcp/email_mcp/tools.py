"""MCP tool definitions and handlers."""

import asyncio
import json
import logging
from typing import Callable

from mcp.types import TextContent, Tool

from . import database as db
from . import sync as sync_module
from .graph import GraphClient

logger = logging.getLogger(__name__)


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="list_folders",
            description="List all mail folders with unread and total message counts.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="search_emails",
            description=(
                "Full-text search across subject, body, and sender fields. "
                "Optionally filter by folder, sender, date range, or read status."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms"},
                    "folder_id": {"type": "string", "description": "Limit to this folder ID"},
                    "sender": {"type": "string", "description": "Filter by sender name or email (partial match)"},
                    "date_from": {"type": "string", "description": "Earliest date (ISO 8601, e.g. 2025-01-01)"},
                    "date_to": {"type": "string", "description": "Latest date (ISO 8601)"},
                    "is_read": {"type": "boolean", "description": "true=read only, false=unread only"},
                    "limit": {"type": "integer", "description": "Max results (default 50, max 200)", "default": 50},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_emails_by_folder",
            description="List emails in a folder, newest first, with metadata and body snippet.",
            inputSchema={
                "type": "object",
                "properties": {
                    "folder_id": {"type": "string", "description": "Folder ID (from list_folders)"},
                    "limit": {"type": "integer", "description": "Results per page (default 50)", "default": 50},
                    "offset": {"type": "integer", "description": "Pagination offset (default 0)", "default": 0},
                },
                "required": ["folder_id"],
            },
        ),
        Tool(
            name="get_email",
            description="Retrieve a single email by ID, including full body and attachment names.",
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "Message ID"},
                },
                "required": ["message_id"],
            },
        ),
        Tool(
            name="get_thread",
            description="Retrieve all messages in a conversation thread, oldest first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string", "description": "Conversation/thread ID"},
                },
                "required": ["conversation_id"],
            },
        ),
        Tool(
            name="create_draft",
            description=(
                "Create a draft email (or reply draft) via Microsoft Graph. "
                "The draft is saved to Drafts folder but not sent."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Recipient email addresses",
                    },
                    "subject": {"type": "string", "description": "Email subject"},
                    "body": {"type": "string", "description": "Email body content"},
                    "body_type": {
                        "type": "string",
                        "enum": ["Text", "HTML"],
                        "default": "Text",
                        "description": "Body format (Text or HTML)",
                    },
                    "cc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "CC recipient email addresses",
                    },
                    "reply_to_id": {
                        "type": "string",
                        "description": "Message ID to reply to — creates a pre-populated reply draft",
                    },
                },
                "required": ["to", "subject", "body"],
            },
        ),
        Tool(
            name="count_messages",
            description="Count emails matching filters (returns a number, optionally grouped). Use for 'how many emails' questions instead of fetching lists.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sender_email": {"type": "string", "description": "Exact sender email"},
                    "sender_contains": {"type": "string", "description": "Substring match against sender name OR email"},
                    "folder_id": {"type": "string", "description": "Restrict to one folder"},
                    "folder_name": {"type": "string", "description": "Folder name substring match"},
                    "subject_contains": {"type": "string", "description": "Substring match against subject"},
                    "date_from": {"type": "string", "description": "ISO 8601 lower bound"},
                    "date_to": {"type": "string", "description": "ISO 8601 upper bound"},
                    "is_read": {"type": "boolean"},
                    "has_attachments": {"type": "boolean"},
                    "group_by": {"type": "string", "enum": ["none", "sender", "folder", "day", "month"], "default": "none"}
                },
                "required": []
            },
        ),
        Tool(
            name="sync_status",
            description="Return sync metadata: last sync times, total message count, and any errors.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="force_sync",
            description="Trigger an immediate incremental (delta) sync in the background.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


def _text(content: str) -> list[TextContent]:
    return [TextContent(type="text", text=content)]


async def handle_tool(
    name: str, arguments: dict, get_token: Callable[[], str]
) -> list[TextContent]:
    try:
        match name:
            case "list_folders":
                return _list_folders()
            case "search_emails":
                return _search_emails(arguments)
            case "get_emails_by_folder":
                return _get_emails_by_folder(arguments)
            case "get_email":
                return _get_email(arguments)
            case "get_thread":
                return _get_thread(arguments)
            case "create_draft":
                return await _create_draft(arguments, get_token)
            case "count_messages":
                return _handle_count_messages(arguments)
            case "sync_status":
                return _sync_status()
            case "force_sync":
                return await _force_sync(get_token)
            case _:
                return _text(f"Unknown tool: {name}")
    except Exception as exc:
        logger.error("Tool %s error: %s", name, exc, exc_info=True)
        return _text(f"Error executing {name}: {exc}")


# ── Tool implementations ──────────────────────────────────────────────────────

def _list_folders() -> list[TextContent]:
    folders = db.get_folders()
    if not folders:
        return _text("No folders found. Run a sync first.")
    lines = []
    for f in folders:
        unread = f["unread_item_count"] or 0
        total = f["total_item_count"] or 0
        unread_tag = f"  ({unread} unread)" if unread else ""
        synced_tag = "" if f["fully_synced"] else "  [not synced]"
        lines.append(f"• {f['display_name']}{unread_tag}  — {total} total{synced_tag}")
        lines.append(f"  ID: {f['id']}")
    return _text("\n".join(lines))


def _search_emails(args: dict) -> list[TextContent]:
    limit = min(int(args.get("limit", 50)), 200)
    results = db.search_messages(
        query=args["query"],
        folder_id=args.get("folder_id"),
        sender=args.get("sender"),
        date_from=args.get("date_from"),
        date_to=args.get("date_to"),
        is_read=args.get("is_read"),
        limit=limit,
    )
    if not results:
        return _text("No results found.")

    lines = [f"Found {len(results)} result(s):\n"]
    for msg in results:
        unread = "" if msg["is_read"] else "[UNREAD] "
        attach = " 📎" if msg["has_attachments"] else ""
        date = (msg["received_datetime"] or "")[:10]
        snippet = (msg.get("snippet") or "").strip()[:160]
        lines.append(
            f"{unread}[{date}] {msg['sender_name']} <{msg['sender_email']}>{attach}\n"
            f"  Subject: {msg['subject']}\n"
            f"  {snippet}\n"
            f"  ID: {msg['id']}\n"
        )
    return _text("\n".join(lines))


def _get_emails_by_folder(args: dict) -> list[TextContent]:
    folder_id = args["folder_id"]
    limit = min(int(args.get("limit", 50)), 200)
    offset = int(args.get("offset", 0))

    messages = db.get_messages_by_folder(folder_id, limit, offset)
    if not messages:
        return _text("No messages found (folder may be empty or not yet synced).")

    lines = []
    for msg in messages:
        unread = "" if msg["is_read"] else "[UNREAD] "
        attach = " 📎" if msg["has_attachments"] else ""
        date = (msg["received_datetime"] or "")[:10]
        snippet = (msg.get("snippet") or "").strip()[:160]
        lines.append(
            f"{unread}[{date}] {msg['sender_name']} <{msg['sender_email']}>{attach}\n"
            f"  Subject: {msg['subject']}\n"
            f"  {snippet}\n"
            f"  ID: {msg['id']}\n"
        )
    return _text("\n".join(lines))


def _get_email(args: dict) -> list[TextContent]:
    msg = db.get_message_by_id(args["message_id"])
    if not msg:
        return _text("Message not found in local cache.")

    to_recipients = json.loads(msg.get("to_recipients") or "[]")
    to_str = ", ".join(
        f"{r.get('name', '')} <{r.get('email', '')}>" for r in to_recipients
    )
    attachments = json.loads(msg.get("attachment_names") or "[]")
    body = (msg.get("body_text") or "").strip()

    return _text(
        f"From:    {msg['sender_name']} <{msg['sender_email']}>\n"
        f"To:      {to_str}\n"
        f"Subject: {msg['subject']}\n"
        f"Date:    {msg['received_datetime']}\n"
        f"Read:    {'Yes' if msg['is_read'] else 'No'}\n"
        f"Attachments: {', '.join(attachments) if attachments else 'None'}\n"
        f"Thread ID: {msg['conversation_id']}\n"
        f"Message ID: {msg['id']}\n"
        f"\n{'─' * 60}\n\n"
        f"{body}"
    )


def _get_thread(args: dict) -> list[TextContent]:
    messages = db.get_thread(args["conversation_id"])
    if not messages:
        return _text("Thread not found in local cache.")

    lines = [f"Thread: {len(messages)} message(s)\n"]
    for i, msg in enumerate(messages, 1):
        body_preview = (msg.get("body_text") or "").strip()[:400]
        lines.append(
            f"{'─' * 40}\n"
            f"Message {i} of {len(messages)}\n"
            f"From:    {msg['sender_name']} <{msg['sender_email']}>\n"
            f"Date:    {msg['received_datetime']}\n"
            f"Subject: {msg['subject']}\n"
            f"\n{body_preview}\n"
        )
    return _text("\n".join(lines))


async def _create_draft(args: dict, get_token: Callable[[], str]) -> list[TextContent]:
    async with GraphClient(get_token) as client:
        result = await client.create_draft(
            to=args["to"],
            subject=args["subject"],
            body=args["body"],
            body_type=args.get("body_type", "Text"),
            cc=args.get("cc"),
            reply_to_id=args.get("reply_to_id"),
        )
    return _text(f"Draft created. ID: {result['id']}")


def _sync_status() -> list[TextContent]:
    s = sync_module.get_sync_status()
    return _text(
        f"Last full sync:  {s['last_full_sync'] or 'Never'}\n"
        f"Last delta sync: {s['last_delta_sync'] or 'Never'}\n"
        f"Total messages:  {s['total_messages']:,}\n"
        f"Sync running:    {'Yes' if s['sync_in_progress'] else 'No'}\n"
        f"Last error:      {s['last_error'] or 'None'}"
    )


async def _force_sync(get_token: Callable[[], str]) -> list[TextContent]:
    asyncio.create_task(sync_module.run_delta_sync(get_token))
    return _text("Delta sync triggered in the background. Check sync_status for progress.")



def _handle_count_messages(args: dict) -> list[TextContent]:
    from . import database as _db
    where = []
    params: list = []

    if args.get("sender_email"):
        where.append("sender_email = ?")
        params.append(args["sender_email"].lower())
    if args.get("sender_contains"):
        v = "%" + args["sender_contains"] + "%"
        where.append("(sender_email LIKE ? OR sender_name LIKE ?)")
        params += [v, v]
    if args.get("folder_id"):
        where.append("folder_id = ?")
        params.append(args["folder_id"])
    if args.get("folder_name"):
        where.append("folder_id IN (SELECT id FROM folders WHERE display_name LIKE ?)")
        params.append("%" + args["folder_name"] + "%")
    if args.get("subject_contains"):
        where.append("subject LIKE ?")
        params.append("%" + args["subject_contains"] + "%")
    if args.get("date_from"):
        where.append("received_datetime >= ?")
        params.append(args["date_from"])
    if args.get("date_to"):
        where.append("received_datetime <= ?")
        params.append(args["date_to"])
    if args.get("is_read") is not None:
        where.append("is_read = ?")
        params.append(1 if args["is_read"] else 0)
    if args.get("has_attachments") is not None:
        where.append("has_attachments = ?")
        params.append(1 if args["has_attachments"] else 0)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    group_by = args.get("group_by", "none")

    with _db.get_conn() as conn:
        if group_by == "none":
            n = conn.execute(f"SELECT COUNT(*) FROM messages{where_sql}", params).fetchone()[0]
            return _text(str(n))

        if group_by == "sender":
            sql = (f"SELECT COALESCE(sender_name, sender_email) AS who, COUNT(*) AS n "
                   f"FROM messages{where_sql} GROUP BY sender_email ORDER BY n DESC LIMIT 50")
        elif group_by == "folder":
            sql = (f"SELECT (SELECT display_name FROM folders f WHERE f.id=m.folder_id), COUNT(*) AS n "
                   f"FROM messages m{where_sql} GROUP BY m.folder_id ORDER BY n DESC LIMIT 50")
        elif group_by == "day":
            sql = (f"SELECT date(received_datetime) AS d, COUNT(*) AS n "
                   f"FROM messages{where_sql} GROUP BY d ORDER BY d DESC LIMIT 90")
        elif group_by == "month":
            sql = (f"SELECT substr(received_datetime, 1, 7) AS m, COUNT(*) AS n "
                   f"FROM messages{where_sql} GROUP BY m ORDER BY m DESC LIMIT 36")
        else:
            return _text("Unknown group_by: " + str(group_by))
        rows = conn.execute(sql, params).fetchall()

    if not rows:
        return _text("0")
    total = sum(r[1] for r in rows)
    lines = ["total: " + str(total), ""]
    for r in rows:
        label = r[0] if r[0] is not None else "(unknown)"
        lines.append("  " + str(label) + ": " + str(r[1]))
    return _text("\n".join(lines))
