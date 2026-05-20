"""MCP tool definitions and handlers."""

import asyncio
import json
import logging
from typing import Callable

from mcp.types import TextContent, Tool

from . import database as db
from . import sync as sync_module

logger = logging.getLogger(__name__)


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="list_folders",
            description="List Granola folders as a tree, with note counts per folder. Orphans (parent points to missing folder) are shown at root tagged [orphaned].",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="list_notes",
            description="List notes newest-first (by created_at). Filters: folder_id, created_after, created_before, attendee_email, title_contains. Returns metadata + 200-char summary snippet (no transcript).",
            inputSchema={
                "type": "object",
                "properties": {
                    "folder_id": {"type": "string"},
                    "created_after": {"type": "string", "description": "ISO timestamp"},
                    "created_before": {"type": "string", "description": "ISO timestamp"},
                    "attendee_email": {"type": "string"},
                    "title_contains": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
        ),
        Tool(
            name="search_notes",
            description="Full-text search across title, summary, attendees, and (cached) transcripts. Returns ranked snippets with matched field annotation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "FTS5 query string"},
                    "folder_id": {"type": "string"},
                    "created_after": {"type": "string"},
                    "created_before": {"type": "string"},
                    "attendee_email": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_note",
            description="Get full detail for a note by ID. Includes title, owner, dates, calendar event, attendees, summary_markdown, folder membership, and transcript availability. Set include_transcript=true to fetch + return transcript text (caches it).",
            inputSchema={
                "type": "object",
                "properties": {
                    "note_id": {"type": "string"},
                    "include_transcript": {"type": "boolean", "default": False},
                },
                "required": ["note_id"],
            },
        ),
        Tool(
            name="get_transcript",
            description="Return the ordered transcript segments for a note. Fetches and caches on first access. If the note is still processing, returns a clear status message.",
            inputSchema={
                "type": "object",
                "properties": {"note_id": {"type": "string"}},
                "required": ["note_id"],
            },
        ),
        Tool(
            name="get_notes_by_folder",
            description="List notes inside a folder. Accepts folder_id OR folder_name (case-insensitive substring match).",
            inputSchema={
                "type": "object",
                "properties": {
                    "folder_id": {"type": "string"},
                    "folder_name": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_notes_by_attendee",
            description="List notes where an attendee matches (by email or display name).",
            inputSchema={
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                    "name": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
        ),
        Tool(
            name="sync_status",
            description="Sync metadata: last sync times, total notes, transcripts cached, pending count, last error.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="force_sync",
            description="Trigger an immediate incremental sync in the background.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


def _text(content: str) -> list[TextContent]:
    return [TextContent(type="text", text=content)]


async def handle_tool(name: str, arguments: dict, get_key: Callable[[], str]) -> list[TextContent]:
    try:
        match name:
            case "list_folders":
                return _list_folders()
            case "list_notes":
                return _list_notes(arguments)
            case "search_notes":
                return _search_notes(arguments)
            case "get_note":
                return await _get_note(arguments, get_key)
            case "get_transcript":
                return await _get_transcript(arguments, get_key)
            case "get_notes_by_folder":
                return _get_notes_by_folder(arguments)
            case "get_notes_by_attendee":
                return _get_notes_by_attendee(arguments)
            case "sync_status":
                return _sync_status()
            case "force_sync":
                return await _force_sync(get_key)
            case _:
                return _text(f"Unknown tool: {name}")
    except Exception as exc:
        logger.error("Tool %s error: %s", name, exc, exc_info=True)
        return _text(f"Error executing {name}: {exc}")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _render_title(note: dict) -> str:
    title = (note.get("title") or "").strip()
    if title:
        return title
    when = note.get("scheduled_start_time") or note.get("created_at") or "unknown time"
    return f"(Untitled meeting — {when})"


def _snippet(text: str | None, n: int = 200) -> str:
    if not text:
        return ""
    s = text.strip().replace("\n", " ")
    return s[:n] + ("…" if len(s) > n else "")


def _format_note_row(n: dict) -> str:
    folder_ids = json.loads(n.get("folder_ids") or "[]")
    return (
        f"• {_render_title(n)}\n"
        f"  id: {n['note_id']}  created: {n.get('created_at') or '?'}  updated: {n.get('updated_at') or '?'}\n"
        f"  owner: {n.get('owner_name') or '?'} <{n.get('owner_email') or ''}>  folders: {len(folder_ids)}\n"
        f"  summary: {_snippet(n.get('summary_text'))}"
    )


# ── Tool impls ───────────────────────────────────────────────────────────────

def _list_folders() -> list[TextContent]:
    folders = db.get_folders()
    if not folders:
        return _text("No folders cached yet. Run a sync first.")

    by_id = {f["id"]: f for f in folders}
    children: dict[str | None, list[dict]] = {}
    orphan_pseudo_root: list[dict] = []
    for f in folders:
        pid = f.get("parent_folder_id")
        if pid and pid not in by_id:
            orphan_pseudo_root.append(f)
            children.setdefault(None, [])  # ensure root exists
            continue
        children.setdefault(pid, []).append(f)

    counts = {fid: db.count_notes_in_folder(fid) for fid in by_id.keys()}

    lines: list[str] = []

    def _walk(parent: str | None, depth: int) -> None:
        for f in sorted(children.get(parent, []), key=lambda x: (x["name"] or "").lower()):
            indent = "  " * depth
            tag = ""
            lines.append(f"{indent}• {f['name'] or '(unnamed)'}{tag}  [{counts.get(f['id'], 0)} notes]  (id: {f['id']})")
            _walk(f["id"], depth + 1)

    _walk(None, 0)
    for f in sorted(orphan_pseudo_root, key=lambda x: (x["name"] or "").lower()):
        lines.append(f"• {f['name'] or '(unnamed)'} [orphaned]  [{counts.get(f['id'], 0)} notes]  (id: {f['id']})")
        _walk(f["id"], 1)

    if not lines:
        return _text("No folders.")
    return _text("\n".join(lines))


def _list_notes(args: dict) -> list[TextContent]:
    rows = db.list_notes(
        folder_id=args.get("folder_id"),
        created_after=args.get("created_after"),
        created_before=args.get("created_before"),
        attendee=args.get("attendee_email"),
        title_contains=args.get("title_contains"),
        limit=min(int(args.get("limit", 50)), 200),
        offset=int(args.get("offset", 0)),
    )
    if not rows:
        return _text("No notes matched.")
    out = [f"Showing {len(rows)} note(s):\n"]
    out += [_format_note_row(r) for r in rows]
    return _text("\n\n".join(out))


def _search_notes(args: dict) -> list[TextContent]:
    query = args.get("query", "").strip()
    if not query:
        return _text("Empty query.")
    rows = db.search_notes_fts(
        query=query,
        folder_id=args.get("folder_id"),
        created_after=args.get("created_after"),
        created_before=args.get("created_before"),
        attendee=args.get("attendee_email"),
        limit=min(int(args.get("limit", 50)), 200),
    )
    if not rows:
        return _text("No notes matched.")
    out = [f"Found {len(rows)} match(es):\n"]
    for r in rows:
        which = []
        if "[" in (r.get("_h_title") or ""):
            which.append("title")
        if "[" in (r.get("_h_summary") or ""):
            which.append("summary")
        if "[" in (r.get("_h_transcript") or ""):
            which.append("transcript")
        if "[" in (r.get("_h_attendees") or ""):
            which.append("attendees")
        which_str = ",".join(which) or "?"
        out.append(
            f"• {_render_title(r)}\n"
            f"  id: {r['note_id']}  matched: {which_str}  created: {r.get('created_at') or '?'}\n"
            f"  snippet: {r.get('_snippet') or ''}"
        )
    return _text("\n\n".join(out))


async def _get_note(args: dict, get_key: Callable[[], str]) -> list[TextContent]:
    note_id = args["note_id"]
    n = db.get_note(note_id)
    if not n:
        return _text(f"Note {note_id} not found in local cache. Try force_sync, or it may still be processing.")

    folder_ids = json.loads(n.get("folder_ids") or "[]")
    attendees = json.loads(n.get("attendees") or "[]")
    attendee_lines = []
    for a in attendees:
        if isinstance(a, dict):
            name = a.get("name") or a.get("displayName") or ""
            email = a.get("email") or ""
            attendee_lines.append(f"  - {name} <{email}>".rstrip())
        elif isinstance(a, str):
            attendee_lines.append(f"  - {a}")

    include_transcript = bool(args.get("include_transcript", False))
    transcript_section = ""
    if include_transcript:
        transcript = await sync_module.ensure_transcript(get_key, note_id)
        if transcript is None:
            transcript_section = "\n\nTranscript: (not yet available — note may still be processing)"
        else:
            text = " ".join((s.get("text") or "").strip() for s in transcript if (s.get("text") or "").strip())
            transcript_section = f"\n\nTranscript ({len(transcript)} segments):\n{text}"
    else:
        avail = "cached" if n.get("transcript_cached") else "not cached (fetch via get_transcript or include_transcript=true)"
        transcript_section = f"\n\nTranscript: {avail}"

    return _text(
        f"Title: {_render_title(n)}\n"
        f"ID: {n['note_id']}\n"
        f"Owner: {n.get('owner_name') or '?'} <{n.get('owner_email') or ''}>\n"
        f"Created: {n.get('created_at') or '?'}  Updated: {n.get('updated_at') or '?'}\n"
        f"Calendar event: {n.get('calendar_event_title') or '—'}  "
        f"({n.get('scheduled_start_time') or '?'} → {n.get('scheduled_end_time') or '?'})\n"
        f"Organiser: {n.get('organiser_email') or '—'}\n"
        f"Folders: {folder_ids or '—'}\n"
        f"Attendees:\n" + ("\n".join(attendee_lines) if attendee_lines else "  —") +
        "\n\n" + (n.get("summary_markdown") or n.get("summary_text") or "(no summary)") +
        transcript_section
    )


async def _get_transcript(args: dict, get_key: Callable[[], str]) -> list[TextContent]:
    note_id = args["note_id"]
    transcript = await sync_module.ensure_transcript(get_key, note_id)
    if transcript is None:
        return _text("Transcript not yet available — note may still be processing. Try again later.")
    if not transcript:
        return _text("Transcript is empty for this note.")
    lines = [f"Transcript: {len(transcript)} segment(s)\n"]
    for seg in transcript:
        sp = seg.get("speaker") or {}
        src = sp.get("source") if isinstance(sp, dict) else ""
        text = (seg.get("text") or "").strip()
        st = seg.get("start_time") or seg.get("startTime") or ""
        et = seg.get("end_time") or seg.get("endTime") or ""
        lines.append(f"[{st} → {et}] ({src or '?'}) {text}")
    return _text("\n".join(lines))


def _get_notes_by_folder(args: dict) -> list[TextContent]:
    folder_id = args.get("folder_id")
    folder_name = args.get("folder_name")

    if not folder_id and folder_name:
        matches = db.find_folders_by_name(folder_name)
        if not matches:
            return _text(f"No folder matched '{folder_name}'.")
        if len(matches) > 1:
            out = ["Multiple folders matched — specify folder_id:"]
            for m in matches:
                out.append(f"  • {m['name']}  (id: {m['id']})")
            return _text("\n".join(out))
        folder_id = matches[0]["id"]

    if not folder_id:
        return _text("Provide folder_id or folder_name.")

    rows = db.list_notes(
        folder_id=folder_id,
        limit=min(int(args.get("limit", 50)), 200),
        offset=int(args.get("offset", 0)),
    )
    if not rows:
        return _text("No notes in that folder.")
    out = [f"Showing {len(rows)} note(s) in folder {folder_id}:\n"]
    out += [_format_note_row(r) for r in rows]
    return _text("\n\n".join(out))


def _get_notes_by_attendee(args: dict) -> list[TextContent]:
    email = (args.get("email") or "").strip()
    name = (args.get("name") or "").strip()
    if not email and not name:
        return _text("Provide email or name.")
    needle = email or name
    rows = db.list_notes(
        attendee=needle,
        limit=min(int(args.get("limit", 50)), 200),
        offset=int(args.get("offset", 0)),
    )
    if not rows:
        return _text(f"No notes found with attendee matching '{needle}'.")
    out = [f"Showing {len(rows)} note(s) with attendee '{needle}':\n"]
    out += [_format_note_row(r) for r in rows]
    return _text("\n\n".join(out))


def _sync_status() -> list[TextContent]:
    s = sync_module.get_sync_status()
    return _text(
        f"Last full sync:        {s['last_full_sync'] or 'Never'}\n"
        f"Last incremental sync: {s['last_incremental_sync'] or 'Never'}\n"
        f"Full sync complete:    {'Yes' if s['full_sync_complete'] else 'No'}\n"
        f"Total notes:           {s['total_notes']:,}\n"
        f"Transcripts cached:    {s['transcripts_cached']:,}\n"
        f"Pending notes:         {s['pending_count']:,}\n"
        f"Sync running:          {'Yes' if s['sync_in_progress'] else 'No'}\n"
        f"Auth dead:             {'Yes — update ~/.granola-mcp/config.json' if s['auth_dead'] else 'No'}\n"
        f"Last error:            {s['last_error'] or 'None'}"
    )


async def _force_sync(get_key: Callable[[], str]) -> list[TextContent]:
    asyncio.create_task(sync_module.run_incremental_sync(get_key))
    return _text("Incremental sync triggered in the background. Check sync_status for progress.")
