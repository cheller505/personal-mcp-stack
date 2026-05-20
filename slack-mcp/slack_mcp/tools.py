"""MCP tool definitions and handlers for the Slack server (read-only)."""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Callable

from mcp.types import TextContent, Tool

from . import database as db
from . import sync as sync_module

logger = logging.getLogger(__name__)


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="list_workspaces",
            description="Show the cached Slack workspace (name, domain, URL).",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="list_channels",
            description=(
                "List Slack conversations (channels / DMs / group DMs / private). "
                "Filters: type, is_archived, member_only (default true), name_contains."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["public_channel", "private_channel", "im", "mpim", "all"]},
                    "is_archived": {"type": "boolean"},
                    "member_only": {"type": "boolean", "default": True},
                    "name_contains": {"type": "string"},
                },
                "required": [],
            },
        ),
        Tool(
            name="list_users",
            description="List Slack users in the workspace.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name_contains": {"type": "string"},
                    "include_deleted": {"type": "boolean", "default": False},
                    "include_bots": {"type": "boolean", "default": False},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_channel_messages",
            description=(
                "Get messages in a channel, newest first. "
                "Resolves user IDs and Slack <@U..>/<#C..> mentions to names."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                    "before_ts": {"type": "string"},
                    "after_ts": {"type": "string"},
                },
                "required": ["channel_id"],
            },
        ),
        Tool(
            name="get_thread",
            description="Get a thread (parent + replies, chronological) by channel and parent ts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "thread_ts": {"type": "string"},
                },
                "required": ["channel_id", "thread_ts"],
            },
        ),
        Tool(
            name="get_message",
            description="Get a single message in full detail by channel_id and ts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "ts": {"type": "string"},
                },
                "required": ["channel_id", "ts"],
            },
        ),
        Tool(
            name="search_messages",
            description=(
                "Full-text search across message text, channel name, and user name. "
                "Optional filters: channel_id, user_id/user_name, from_ts, to_ts, "
                "channel_type, is_thread."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "channel_id": {"type": "string"},
                    "user_id": {"type": "string"},
                    "user_name": {"type": "string"},
                    "from_ts": {"type": "string"},
                    "to_ts": {"type": "string"},
                    "channel_type": {"type": "string", "enum": ["public_channel", "private_channel", "im", "mpim"]},
                    "is_thread": {"type": "boolean"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_messages_from_user",
            description="Recent messages authored by a user, across all channels.",
            inputSchema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "user_name": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_messages_to_user",
            description="DMs with a specific user (resolves to their `im` channel).",
            inputSchema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "user_name": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_user",
            description="Look up a user by ID, login, real name, display name, or email substring.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="count_messages",
            description="Count messages matching filters (returns a number, optionally grouped). Use for 'how many' questions instead of fetching messages and counting client-side.",
            inputSchema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "Slack user ID of sender"},
                    "user_name": {"type": "string", "description": "Slack handle of sender (e.g. kooper)"},
                    "channel_id": {"type": "string", "description": "Restrict to one channel ID"},
                    "channel_name": {"type": "string", "description": "Channel name substring (case-insensitive)"},
                    "channel_type": {"type": "string", "enum": ["im", "mpim", "public_channel", "private_channel"]},
                    "with_user_name": {"type": "string", "description": "Count in 1:1 DM with this user (handle)"},
                    "with_user_id": {"type": "string", "description": "Count in 1:1 DM with this user (id)"},
                    "from_ts": {"type": "string", "description": "Slack ts lower bound (inclusive)"},
                    "to_ts": {"type": "string", "description": "Slack ts upper bound (inclusive)"},
                    "group_by": {"type": "string", "enum": ["none", "channel", "user", "day"], "default": "none"}
                },
                "required": []
            },
        ),
        Tool(
            name="sync_status",
            description="Return sync metadata: workspace name, totals, last sync times, errors.",
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


async def handle_tool(
    name: str, arguments: dict, get_token: Callable[[], str]
) -> list[TextContent]:
    try:
        match name:
            case "list_workspaces":
                return _list_workspaces()
            case "list_channels":
                return _list_channels(arguments)
            case "list_users":
                return _list_users(arguments)
            case "get_channel_messages":
                return _get_channel_messages(arguments)
            case "get_thread":
                return _get_thread(arguments)
            case "get_message":
                return _get_message(arguments)
            case "search_messages":
                return _search_messages(arguments)
            case "get_messages_from_user":
                return _get_messages_from_user(arguments)
            case "get_messages_to_user":
                return _get_messages_to_user(arguments)
            case "get_user":
                return _get_user(arguments)
            case "count_messages":
                return _count_messages(arguments)
            case "sync_status":
                return _sync_status()
            case "force_sync":
                return await _force_sync(get_token)
            case _:
                return _text(f"Unknown tool: {name}")
    except Exception as exc:
        logger.error("Tool %s error: %s", name, exc, exc_info=True)
        return _text(f"Error executing {name}: {exc}")


# ── Helpers ───────────────────────────────────────────────────────────────────

_MENTION_USER = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
_MENTION_CHAN = re.compile(r"<#([CG][A-Z0-9]+)(?:\|([^>]*))?>")
_LINK_RE = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")


def _user_display(u: dict | None) -> str:
    if not u:
        return ""
    return u.get("display_name") or u.get("real_name") or u.get("name") or u.get("id") or ""


def _resolve_text(text: str, users_map: dict[str, dict],
                  channels_map: dict[str, dict]) -> str:
    if not text:
        return ""

    def sub_user(m: re.Match) -> str:
        uid = m.group(1)
        u = users_map.get(uid)
        return f"@{_user_display(u) or uid}"

    def sub_chan(m: re.Match) -> str:
        cid = m.group(1)
        fallback = m.group(2)
        c = channels_map.get(cid)
        name = (c.get("name") if c else None) or fallback or cid
        return f"#{name}"

    def sub_link(m: re.Match) -> str:
        url = m.group(1)
        label = m.group(2)
        return f"{label} ({url})" if label else url

    out = _MENTION_USER.sub(sub_user, text)
    out = _MENTION_CHAN.sub(sub_chan, out)
    out = _LINK_RE.sub(sub_link, out)
    return out


def _format_ts(ts: str) -> str:
    try:
        secs = float(ts)
        return datetime.fromtimestamp(secs, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError):
        return ts


def _reactions_summary(reactions_json: str) -> str:
    try:
        items = json.loads(reactions_json or "[]")
    except Exception:
        return ""
    parts = [f":{r.get('name', '?')}:×{r.get('count', 0)}" for r in items]
    return " ".join(parts)


def _file_names(files_json: str) -> list[str]:
    try:
        items = json.loads(files_json or "[]")
    except Exception:
        return []
    return [f.get("name") or f.get("title") or f.get("id", "?") for f in items]


def _channel_label(conv: dict, users_map: dict[str, dict]) -> str:
    t = conv.get("type")
    if t == "im":
        u = users_map.get(conv.get("user_id") or "") or {}
        return f"DM: {_user_display(u) or conv.get('user_id') or conv['id']}"
    if t == "mpim":
        # MPIM names look like "mpdm-alice--bob--carol-1"; try to expand using
        # the embedded user logins.
        raw = conv.get("name") or ""
        members = []
        if raw.startswith("mpdm-"):
            for piece in raw[len("mpdm-"):].split("--"):
                piece = piece.rstrip("-0123456789")
                # Match by login (`name`)
                match = next(
                    (u for u in users_map.values() if u.get("name") == piece),
                    None,
                )
                members.append(_user_display(match) if match else piece)
        return "Group DM: " + ", ".join(members) if members else f"Group DM: {raw}"
    return conv.get("name") or conv["id"]


# ── Tool implementations ──────────────────────────────────────────────────────

def _list_workspaces() -> list[TextContent]:
    ws = db.get_workspace()
    if not ws:
        return _text("No workspace cached yet. Run a sync first.")
    return _text(
        f"Workspace: {ws.get('name') or '?'}\n"
        f"  Domain: {ws.get('domain') or '?'}\n"
        f"  URL:    {ws.get('url') or '?'}\n"
        f"  ID:     {ws.get('id') or '?'}\n"
        f"  Synced: {ws.get('synced_at') or '?'}"
    )


def _list_channels(args: dict) -> list[TextContent]:
    type_filter = args.get("type")
    convs = db.get_conversations(
        type=type_filter,
        is_archived=args.get("is_archived"),
        member_only=bool(args.get("member_only", True)),
        name_contains=args.get("name_contains"),
    )
    if not convs:
        return _text("No conversations match.")

    users_map = db.get_users_map()
    lines = [f"{len(convs)} conversation(s):\n"]
    for c in convs:
        label = _channel_label(c, users_map)
        topic = (c.get("topic") or "").strip().replace("\n", " ")
        if len(topic) > 80:
            topic = topic[:80] + "..."
        archived = " [archived]" if c.get("is_archived") else ""
        members = c.get("num_members") or 0
        lines.append(
            f"• [{c['type']}] {label}{archived}  ({members} members)\n"
            f"  ID: {c['id']}"
            + (f"\n  Topic: {topic}" if topic else "")
        )
    return _text("\n".join(lines))


def _list_users(args: dict) -> list[TextContent]:
    users = db.get_users(
        include_deleted=bool(args.get("include_deleted", False)),
        include_bots=bool(args.get("include_bots", False)),
        name_contains=args.get("name_contains"),
    )
    if not users:
        return _text("No users match.")

    lines = [f"{len(users)} user(s):\n"]
    for u in users:
        flags = []
        if u.get("is_bot"):
            flags.append("bot")
        if u.get("is_deleted"):
            flags.append("deleted")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        email = f"  <{u['email']}>" if u.get("email") else ""
        lines.append(
            f"• {_user_display(u)} (@{u.get('name') or '?'}){email}{flag_str}\n"
            f"  ID: {u['id']}"
        )
    return _text("\n".join(lines))


def _render_message_lines(messages: list[dict], users_map: dict[str, dict],
                          channels_map: dict[str, dict],
                          snippet_chars: int = 400) -> list[str]:
    out: list[str] = []
    for m in messages:
        ts = m["ts"]
        when = _format_ts(ts)
        user = users_map.get(m.get("user_id") or "") or {}
        author = _user_display(user) or m.get("user_id") or "(unknown)"
        text = _resolve_text(m.get("text") or "", users_map, channels_map)
        if len(text) > snippet_chars:
            text = text[:snippet_chars].rstrip() + "..."
        reacts = _reactions_summary(m.get("reactions_json") or "[]")
        files = _file_names(m.get("files_json") or "[]")
        replies = int(m.get("reply_count") or 0)

        line = f"[{when}] {author}: {text}"
        extras = []
        if files:
            extras.append("files: " + ", ".join(files))
        if reacts:
            extras.append("reactions: " + reacts)
        if replies:
            extras.append(f"{replies} repl{'ies' if replies != 1 else 'y'}")
        if extras:
            line += "\n  (" + " | ".join(extras) + ")"
        line += f"\n  ts: {ts}"
        out.append(line)
    return out


def _get_channel_messages(args: dict) -> list[TextContent]:
    channel_id = args["channel_id"]
    limit = min(int(args.get("limit", 50)), 500)
    msgs = db.get_channel_messages(
        channel_id,
        limit=limit,
        before_ts=args.get("before_ts"),
        after_ts=args.get("after_ts"),
    )
    if not msgs:
        return _text("No messages found (channel may be empty or not yet synced).")

    users_map = db.get_users_map()
    channels_map = {c["id"]: c for c in db.get_all_conversations()}
    conv = channels_map.get(channel_id)
    label = _channel_label(conv, users_map) if conv else channel_id

    lines = [f"Channel: {label}  ({len(msgs)} messages, newest first)\n"]
    lines.extend(_render_message_lines(msgs, users_map, channels_map))
    return _text("\n\n".join(lines))


def _get_thread(args: dict) -> list[TextContent]:
    channel_id = args["channel_id"]
    thread_ts = args["thread_ts"]
    msgs = db.get_thread_messages(channel_id, thread_ts)
    if not msgs:
        return _text("Thread not found in local cache.")

    users_map = db.get_users_map()
    channels_map = {c["id"]: c for c in db.get_all_conversations()}

    lines = [f"Thread: {len(msgs)} message(s) (chronological)\n"]
    lines.extend(_render_message_lines(msgs, users_map, channels_map, snippet_chars=2000))
    return _text("\n\n".join(lines))


def _get_message(args: dict) -> list[TextContent]:
    msg = db.get_message(args["channel_id"], args["ts"])
    if not msg:
        return _text("Message not found in local cache.")

    users_map = db.get_users_map()
    channels_map = {c["id"]: c for c in db.get_all_conversations()}
    user = users_map.get(msg.get("user_id") or "") or {}
    text = _resolve_text(msg.get("text") or "", users_map, channels_map)
    files = _file_names(msg.get("files_json") or "[]")
    reacts = _reactions_summary(msg.get("reactions_json") or "[]")

    parent_tag = ""
    if msg.get("thread_ts") and msg["thread_ts"] != msg["ts"]:
        parent_tag = f"  (thread reply to ts {msg['thread_ts']})"

    return _text(
        f"Channel:  {msg['channel_id']}\n"
        f"From:     {_user_display(user) or msg.get('user_id') or '(unknown)'}\n"
        f"When:     {_format_ts(msg['ts'])}\n"
        f"ts:       {msg['ts']}{parent_tag}\n"
        f"Replies:  {msg.get('reply_count') or 0}\n"
        f"Files:    {', '.join(files) if files else 'None'}\n"
        f"Reactions: {reacts or 'None'}\n"
        f"Subtype:  {msg.get('subtype') or '-'}\n"
        f"\n{'─' * 60}\n\n"
        f"{text}"
    )


def _search_messages(args: dict) -> list[TextContent]:
    limit = min(int(args.get("limit", 50)), 500)

    user_id = args.get("user_id")
    if not user_id and args.get("user_name"):
        u = db.find_user(args["user_name"])
        if u:
            user_id = u["id"]

    results = db.search_messages(
        query=args["query"],
        channel_id=args.get("channel_id"),
        user_id=user_id,
        from_ts=args.get("from_ts"),
        to_ts=args.get("to_ts"),
        channel_type=args.get("channel_type"),
        is_thread=args.get("is_thread"),
        limit=limit,
    )
    if not results:
        return _text("No results found.")

    users_map = db.get_users_map()
    channels_map = {c["id"]: c for c in db.get_all_conversations()}

    lines = [f"Found {len(results)} result(s):\n"]
    for r in results:
        author = _user_display(users_map.get(r.get("user_id") or ""))
        conv = channels_map.get(r["channel_id"])
        chan_label = _channel_label(conv, users_map) if conv else r["channel_id"]
        snippet = _resolve_text(r.get("snippet") or "", users_map, channels_map)
        lines.append(
            f"[{_format_ts(r['ts'])}] #{chan_label} — {author or '(unknown)'}\n"
            f"  {snippet}\n"
            f"  channel_id={r['channel_id']}  ts={r['ts']}"
        )
    return _text("\n\n".join(lines))


def _resolve_user_arg(args: dict) -> dict | None:
    if args.get("user_id"):
        u = db.get_user(args["user_id"])
        if u:
            return u
    if args.get("user_name"):
        return db.find_user(args["user_name"])
    return None


def _get_messages_from_user(args: dict) -> list[TextContent]:
    user = _resolve_user_arg(args)
    if not user:
        return _text("User not found. Provide user_id or user_name.")
    limit = min(int(args.get("limit", 50)), 500)
    msgs = db.get_messages_by_user(user["id"], limit=limit)
    if not msgs:
        return _text(f"No messages cached for {_user_display(user)}.")

    users_map = db.get_users_map()
    channels_map = {c["id"]: c for c in db.get_all_conversations()}

    lines = [f"Recent messages from {_user_display(user)} ({len(msgs)}):\n"]
    for m in msgs:
        conv = channels_map.get(m["channel_id"])
        chan_label = _channel_label(conv, users_map) if conv else m["channel_id"]
        text = _resolve_text(m.get("text") or "", users_map, channels_map)
        if len(text) > 240:
            text = text[:240].rstrip() + "..."
        lines.append(
            f"[{_format_ts(m['ts'])}] #{chan_label}\n"
            f"  {text}\n"
            f"  channel_id={m['channel_id']}  ts={m['ts']}"
        )
    return _text("\n\n".join(lines))


def _get_messages_to_user(args: dict) -> list[TextContent]:
    user = _resolve_user_arg(args)
    if not user:
        return _text("User not found. Provide user_id or user_name.")
    im = db.find_im_channel(user["id"])
    if not im:
        return _text(
            f"No DM channel cached with {_user_display(user)}. "
            "(May not exist, or not yet synced.)"
        )
    limit = min(int(args.get("limit", 50)), 500)
    msgs = db.get_channel_messages(im["id"], limit=limit)
    if not msgs:
        return _text(f"DM with {_user_display(user)} has no cached messages.")

    users_map = db.get_users_map()
    channels_map = {c["id"]: c for c in db.get_all_conversations()}

    lines = [f"DM with {_user_display(user)}  ({len(msgs)} messages, newest first):\n"]
    lines.extend(_render_message_lines(msgs, users_map, channels_map))
    return _text("\n\n".join(lines))


def _get_user(args: dict) -> list[TextContent]:
    u = db.find_user(args["query"])
    if not u:
        return _text("User not found.")
    flags = []
    if u.get("is_bot"):
        flags.append("bot")
    if u.get("is_deleted"):
        flags.append("deleted")
    flag_str = f"  [{', '.join(flags)}]" if flags else ""
    return _text(
        f"{_user_display(u)} (@{u.get('name') or '?'}){flag_str}\n"
        f"  ID:     {u['id']}\n"
        f"  Email:  {u.get('email') or '-'}\n"
        f"  Real:   {u.get('real_name') or '-'}\n"
        f"  Title:  {u.get('profile_title') or '-'}\n"
        f"  TZ:     {u.get('tz') or '-'}"
    )


def _sync_status() -> list[TextContent]:
    s = sync_module.get_sync_status()
    ws_name = s.get("workspace_name") or "(unknown)"
    convs = db.get_all_conversations()
    users = db.get_users(include_deleted=True, include_bots=True)
    return _text(
        f"Workspace:           {ws_name}\n"
        f"Total channels:      {len(convs)}\n"
        f"Fully-synced channels: {s['fully_synced_channels']}\n"
        f"Total users:         {len(users)}\n"
        f"Total messages:      {s['total_messages']:,}\n"
        f"Last full sync:      {s['last_full_sync'] or 'Never'}\n"
        f"Last delta sync:     {s['last_delta_sync'] or 'Never'}\n"
        f"Sync running:        {'Yes' if s['sync_in_progress'] else 'No'}\n"
        f"Last error:          {s['last_error'] or 'None'}"
    )


async def _force_sync(get_token: Callable[[], str]) -> list[TextContent]:
    asyncio.create_task(sync_module.run_delta_sync(get_token))
    return _text("Delta sync triggered in the background. Check sync_status for progress.")



def _count_messages(args: dict) -> list[TextContent]:
    from . import database as _db
    where = []
    params: list = []

    with_user_id = args.get("with_user_id")
    with_user_name = args.get("with_user_name")
    if with_user_id or with_user_name:
        with _db.get_conn() as conn:
            if with_user_id:
                row = conn.execute(
                    "SELECT id FROM conversations WHERE type='im' AND user_id=?",
                    (with_user_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT c.id FROM conversations c JOIN users u ON u.id=c.user_id "
                    "WHERE c.type='im' AND u.name=?",
                    (with_user_name,)
                ).fetchone()
        if not row:
            return _text("0  (no DM channel found for that user)")
        where.append("m.channel_id = ?")
        params.append(row[0])

    if args.get("user_id"):
        where.append("m.user_id = ?")
        params.append(args["user_id"])
    if args.get("user_name"):
        where.append("m.user_id = (SELECT id FROM users WHERE name = ?)")
        params.append(args["user_name"])
    if args.get("channel_id"):
        where.append("m.channel_id = ?")
        params.append(args["channel_id"])
    if args.get("channel_name"):
        where.append("m.channel_id IN (SELECT id FROM conversations WHERE name LIKE ?)")
        params.append("%" + args["channel_name"] + "%")
    if args.get("channel_type"):
        where.append("m.channel_id IN (SELECT id FROM conversations WHERE type = ?)")
        params.append(args["channel_type"])
    if args.get("from_ts"):
        where.append("CAST(m.ts AS REAL) >= ?")
        params.append(float(args["from_ts"]))
    if args.get("to_ts"):
        where.append("CAST(m.ts AS REAL) <= ?")
        params.append(float(args["to_ts"]))

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    group_by = args.get("group_by", "none")

    with _db.get_conn() as conn:
        if group_by == "none":
            n = conn.execute("SELECT COUNT(*) FROM messages m" + where_sql, params).fetchone()[0]
            return _text(str(n))
        if group_by == "channel":
            sql = ("SELECT COALESCE(c.name, c.id), COUNT(*) AS n "
                   "FROM messages m LEFT JOIN conversations c ON c.id=m.channel_id"
                   + where_sql + " GROUP BY m.channel_id ORDER BY n DESC LIMIT 50")
        elif group_by == "user":
            sql = ("SELECT COALESCE(u.real_name, u.name, m.user_id), COUNT(*) AS n "
                   "FROM messages m LEFT JOIN users u ON u.id=m.user_id"
                   + where_sql + " GROUP BY m.user_id ORDER BY n DESC LIMIT 50")
        elif group_by == "day":
            sql = ("SELECT date(CAST(m.ts AS REAL),'unixepoch') AS d, COUNT(*) AS n "
                   "FROM messages m" + where_sql + " GROUP BY d ORDER BY d DESC LIMIT 90")
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
