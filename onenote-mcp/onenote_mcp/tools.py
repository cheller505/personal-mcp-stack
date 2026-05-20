"""MCP tool definitions and handlers for OneNote."""

import asyncio
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
            name="list_notebooks",
            description="List all OneNote notebooks. Shows owned vs shared status, user role, last-modified date, and notebook ID.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="list_sections",
            description=(
                "List sections in a notebook, including nested section groups, rendered as an indented tree."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "notebook_id": {"type": "string", "description": "Notebook ID (from list_notebooks)"},
                },
                "required": ["notebook_id"],
            },
        ),
        Tool(
            name="list_pages",
            description="List pages in a section, newest first. Returns title, modified date, ID, and snippet if content cached.",
            inputSchema={
                "type": "object",
                "properties": {
                    "section_id": {"type": "string", "description": "Section ID (from list_sections)"},
                    "limit": {"type": "integer", "description": "Results per page (default 50, max 200)", "default": 50},
                    "offset": {"type": "integer", "description": "Pagination offset (default 0)", "default": 0},
                },
                "required": ["section_id"],
            },
        ),
        Tool(
            name="get_page",
            description=(
                "Fetch and return one page's full content. Fetches HTML from Graph if not already cached, "
                "strips it to text, and caches both. Long bodies are truncated at 8000 chars unless include_html is true."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Page ID"},
                    "include_html": {
                        "type": "boolean",
                        "description": "If true, return raw HTML instead of plain text",
                        "default": False,
                    },
                },
                "required": ["page_id"],
            },
        ),
        Tool(
            name="search_pages",
            description=(
                "Full-text search across page titles and cached body content using SQLite FTS5. "
                "IMPORTANT: only pages whose content has been fetched (via get_page or get_recent_pages or a cache prewarm) "
                "are searchable in the body. Title is always searchable. "
                "Filters: notebook_id, section_id, is_shared (true/false), modified_after/before."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "FTS5 query (terms, AND/OR/NOT, phrase \"...\")"},
                    "notebook_id": {"type": "string", "description": "Limit to one notebook"},
                    "section_id": {"type": "string", "description": "Limit to one section"},
                    "is_shared": {"type": "boolean", "description": "Filter by shared (true) or owned (false)"},
                    "modified_after": {"type": "string", "description": "ISO 8601 lower bound (inclusive)"},
                    "modified_before": {"type": "string", "description": "ISO 8601 upper bound (inclusive)"},
                    "limit": {"type": "integer", "description": "Max results (default 50, max 200)", "default": 50},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_recent_pages",
            description="Most recently modified pages across all notebooks. Optional notebook_id filter.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max results (default 25, max 200)", "default": 25},
                    "notebook_id": {"type": "string", "description": "Limit to one notebook"},
                },
                "required": [],
            },
        ),
        Tool(
            name="create_page",
            description=(
                "Create a new OneNote page in the given section. Provide either body_html (raw HTML) or "
                "body_text (auto-wrapped in <p> tags). Refuses to write to shared notebooks since the granted "
                "Notes.ReadWrite scope only permits writes to notebooks the user owns."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "section_id": {"type": "string", "description": "Section ID to create the page in"},
                    "title": {"type": "string", "description": "Page title"},
                    "body_html": {"type": "string", "description": "Page body as HTML"},
                    "body_text": {"type": "string", "description": "Page body as plain text (wrapped in <p>)"},
                },
                "required": ["section_id", "title"],
            },
        ),
        Tool(
            name="append_to_page",
            description=(
                "Append a block of HTML/text to an existing page's body. Refuses if the page's parent notebook is shared."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Page ID to append to"},
                    "content": {"type": "string", "description": "HTML or text content to append"},
                    "target": {
                        "type": "string",
                        "description": "OneNote target element (default 'body')",
                        "default": "body",
                    },
                },
                "required": ["page_id", "content"],
            },
        ),
        Tool(
            name="replace_page_content",
            description=(
                "Replace the entire body of a page using the OneNote PATCH 'replace' action. "
                "Refuses if the page's parent notebook is shared."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "Page ID to overwrite"},
                    "content": {"type": "string", "description": "New HTML body"},
                },
                "required": ["page_id", "content"],
            },
        ),
        Tool(
            name="sync_status",
            description="Return sync metadata: last sync times, cached counts, last error, and whether a sync is running.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="force_sync",
            description="Trigger an immediate delta sync in the background. Returns immediately.",
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
            case "list_notebooks":
                return _list_notebooks()
            case "list_sections":
                return _list_sections(arguments)
            case "list_pages":
                return _list_pages(arguments)
            case "get_page":
                return await _get_page(arguments, get_token)
            case "search_pages":
                return _search_pages(arguments)
            case "get_recent_pages":
                return _get_recent_pages(arguments)
            case "create_page":
                return await _create_page(arguments, get_token)
            case "append_to_page":
                return await _append_to_page(arguments, get_token)
            case "replace_page_content":
                return await _replace_page_content(arguments, get_token)
            case "sync_status":
                return _sync_status()
            case "force_sync":
                return await _force_sync(get_token)
            case _:
                return _text(f"Unknown tool: {name}")
    except Exception as exc:
        logger.error("Tool %s error: %s", name, exc, exc_info=True)
        return _text(f"Error executing {name}: {exc}")


# ── helpers ──────────────────────────────────────────────────────────────────

def _is_notebook_shared(notebook_id: str | None) -> bool:
    if not notebook_id:
        return False
    nb = db.get_notebook(notebook_id)
    return bool(nb and nb.get("is_shared"))


def _page_notebook_id(page_id: str) -> str | None:
    page = db.get_page(page_id)
    return page["notebook_id"] if page else None


# ── Tool implementations ──────────────────────────────────────────────────────

def _list_notebooks() -> list[TextContent]:
    notebooks = db.get_notebooks()
    if not notebooks:
        return _text("No notebooks found. Run a sync first.")
    lines = []
    for nb in notebooks:
        share_tag = "shared" if nb["is_shared"] else "owned"
        default_tag = " [default]" if nb["is_default"] else ""
        role = nb.get("user_role") or "?"
        mod = (nb.get("modified_at") or "")[:19]
        lines.append(
            f"• {nb['display_name']}  ({share_tag}, role={role}){default_tag}\n"
            f"  Modified: {mod}\n"
            f"  ID: {nb['id']}"
        )
    return _text("\n".join(lines))


def _list_sections(args: dict) -> list[TextContent]:
    notebook_id = args["notebook_id"]
    nb = db.get_notebook(notebook_id)
    if not nb:
        return _text("Notebook not found in cache. Run sync_status / force_sync.")

    lines = [f"Notebook: {nb['display_name']}"]

    def _render_section(sec: dict, indent: int) -> None:
        pad = "  " * indent
        lines.append(
            f"{pad}• {sec['display_name']}  [section]"
            f"{'' if sec['fully_synced'] else '  (not fully synced)'}"
        )
        lines.append(f"{pad}  ID: {sec['id']}")

    def _render_group(group: dict, indent: int) -> None:
        pad = "  " * indent
        lines.append(f"{pad}▾ {group['display_name']}  [section group]")
        lines.append(f"{pad}  ID: {group['id']}")
        for sec in db.get_sections_in_group(group["id"]):
            _render_section(sec, indent + 1)
        for sub in db.get_section_groups_in_group(group["id"]):
            _render_group(sub, indent + 1)

    # Top-level sections (no section group)
    top_sections = db.get_sections_in_notebook(notebook_id)
    for s in top_sections:
        _render_section(s, 1)

    # Top-level section groups
    top_groups = db.get_section_groups_in_notebook(notebook_id)
    for g in top_groups:
        _render_group(g, 1)

    if len(lines) == 1:
        lines.append("  (no sections found)")
    return _text("\n".join(lines))


def _list_pages(args: dict) -> list[TextContent]:
    section_id = args["section_id"]
    limit = min(int(args.get("limit", 50)), 200)
    offset = int(args.get("offset", 0))

    pages = db.get_pages_in_section(section_id, limit, offset)
    if not pages:
        return _text("No pages in this section (or section not yet synced).")

    lines = []
    for p in pages:
        mod = (p.get("modified_at") or "")[:19]
        snippet = (p.get("snippet") or "").strip()[:160]
        cached_tag = "" if p.get("has_content") else "  [content not cached]"
        lines.append(
            f"• {p['title']}{cached_tag}\n"
            f"  Modified: {mod}\n"
            f"  ID: {p['id']}"
            + (f"\n  {snippet}" if snippet else "")
        )
    return _text("\n".join(lines))


async def _get_page(
    args: dict, get_token: Callable[[], str]
) -> list[TextContent]:
    page_id = args["page_id"]
    include_html = bool(args.get("include_html", False))

    page = await sync_module.ensure_page_content(page_id, get_token)
    if not page:
        return _text("Page not found.")

    nb = db.get_notebook(page.get("notebook_id") or "") or {}
    sec = db.get_section(page.get("section_id") or "") or {}

    header = (
        f"Title:    {page.get('title') or '(untitled)'}\n"
        f"Notebook: {nb.get('display_name', '(unknown)')}\n"
        f"Section:  {sec.get('display_name', '(unknown)')}\n"
        f"Created:  {page.get('created_at', '')}\n"
        f"Modified: {page.get('modified_at', '')}\n"
        f"Page ID:  {page['id']}\n"
        f"{'-' * 60}\n\n"
    )

    if include_html:
        body = page.get("content_html") or ""
        return _text(header + body)

    body = page.get("content_text") or ""
    if len(body) > 8000:
        body = body[:8000] + "\n\n[truncated — full body is longer; use include_html=true to get raw HTML]"
    return _text(header + body)


def _search_pages(args: dict) -> list[TextContent]:
    limit = min(int(args.get("limit", 50)), 200)
    results = db.search_pages(
        query=args["query"],
        notebook_id=args.get("notebook_id"),
        section_id=args.get("section_id"),
        is_shared=args.get("is_shared"),
        modified_after=args.get("modified_after"),
        modified_before=args.get("modified_before"),
        limit=limit,
    )
    if not results:
        return _text(
            "No results. Note: only pages whose content has been fetched "
            "(via get_page / get_recent_pages) are searchable in the body. "
            "Titles are always searchable."
        )

    lines = [f"Found {len(results)} result(s):\n"]
    for r in results:
        mod = (r.get("modified_at") or "")[:19]
        snip = (r.get("snippet") or "").strip()
        lines.append(
            f"• {r['title']}\n"
            f"  Modified: {mod}\n"
            f"  Snippet:  {snip}\n"
            f"  ID: {r['id']}"
        )
    return _text("\n".join(lines))


def _get_recent_pages(args: dict) -> list[TextContent]:
    limit = min(int(args.get("limit", 25)), 200)
    notebook_id = args.get("notebook_id")
    pages = db.get_recent_pages(limit=limit, notebook_id=notebook_id)
    if not pages:
        return _text("No recent pages cached.")

    lines = []
    for p in pages:
        mod = (p.get("modified_at") or "")[:19]
        nb = db.get_notebook(p.get("notebook_id") or "") or {}
        lines.append(
            f"• {p['title']}\n"
            f"  Notebook: {nb.get('display_name', '(unknown)')}\n"
            f"  Modified: {mod}\n"
            f"  ID: {p['id']}"
        )
    return _text("\n".join(lines))


async def _create_page(
    args: dict, get_token: Callable[[], str]
) -> list[TextContent]:
    section_id = args["section_id"]
    title = args["title"]

    section = db.get_section(section_id)
    if not section:
        return _text("Section not found in cache. Run sync first.")
    if _is_notebook_shared(section.get("parent_notebook_id")):
        return _text(
            "Refusing to create page: parent notebook is shared. "
            "The granted Notes.ReadWrite scope only permits writes to notebooks you own. "
            "Writes to shared notebooks require Notes.ReadWrite.All which was not granted."
        )

    body_html = args.get("body_html")
    body_text = args.get("body_text")
    if not body_html and body_text:
        # Naive escape into <p> blocks
        from html import escape
        paragraphs = "".join(
            f"<p>{escape(line)}</p>" for line in body_text.splitlines() if line.strip()
        ) or "<p></p>"
        body_html = paragraphs
    if not body_html:
        body_html = "<p></p>"

    from html import escape
    html = (
        "<!DOCTYPE html><html><head>"
        f"<title>{escape(title)}</title>"
        "</head><body>"
        f"{body_html}"
        "</body></html>"
    )

    async with GraphClient(get_token) as client:
        result = await client.create_page(section_id, html)
    return _text(f"Page created. ID: {result.get('id', '(unknown)')}")


async def _append_to_page(
    args: dict, get_token: Callable[[], str]
) -> list[TextContent]:
    page_id = args["page_id"]
    content = args["content"]
    target = args.get("target", "body")

    if _is_notebook_shared(_page_notebook_id(page_id)):
        return _text(
            "Refusing to append: parent notebook is shared. "
            "Notes.ReadWrite cannot modify shared notebooks."
        )

    # Wrap plain text in <p> if it doesn't look like HTML
    payload = content if "<" in content else f"<p>{content}</p>"
    commands = [{"target": target, "action": "append", "content": payload}]

    async with GraphClient(get_token) as client:
        await client.update_page_content(page_id, commands)
    return _text(f"Appended content to page {page_id}.")


async def _replace_page_content(
    args: dict, get_token: Callable[[], str]
) -> list[TextContent]:
    page_id = args["page_id"]
    content = args["content"]

    if _is_notebook_shared(_page_notebook_id(page_id)):
        return _text(
            "Refusing to replace content: parent notebook is shared. "
            "Notes.ReadWrite cannot modify shared notebooks."
        )

    commands = [{"target": "body", "action": "replace", "content": content}]
    async with GraphClient(get_token) as client:
        await client.update_page_content(page_id, commands)
    return _text(f"Replaced body of page {page_id}.")


def _sync_status() -> list[TextContent]:
    s = sync_module.get_sync_status()
    return _text(
        f"Last full sync:      {s['last_full_sync'] or 'Never'}\n"
        f"Last delta sync:     {s['last_delta_sync'] or 'Never'}\n"
        f"Last structure sync: {s['last_structure_sync'] or 'Never'}\n"
        f"Notebooks:           {s['notebooks']:,}\n"
        f"Sections:            {s['sections']:,}\n"
        f"Pages:               {s['pages']:,}\n"
        f"Pages with content:  {s['pages_with_content']:,}\n"
        f"Sync running:        {'Yes' if s['sync_in_progress'] else 'No'}\n"
        f"Last error:          {s['last_error'] or 'None'}"
    )


async def _force_sync(get_token: Callable[[], str]) -> list[TextContent]:
    asyncio.create_task(sync_module.run_delta_sync(get_token))
    return _text("Delta sync triggered in the background. Check sync_status for progress.")
