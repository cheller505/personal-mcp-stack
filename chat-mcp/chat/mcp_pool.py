"""Persistent pool of MCP SSE clients.

One ClientSession per configured server, held open for the app lifetime.
Tools are flattened into a single OpenAI tool list with `<server>_<tool>` names;
calls route back to the right session by splitting on the first underscore.
"""

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client

logger = logging.getLogger(__name__)


@dataclass
class _ServerEntry:
    name: str
    url: str
    session: ClientSession | None = None
    stack: AsyncExitStack | None = None
    tools: list[Any] = field(default_factory=list)  # mcp.types.Tool
    connected: bool = False
    error: str | None = None


class MCPPool:
    def __init__(self, servers: dict[str, str], hidden_tools: set | None = None) -> None:
        self._servers: dict[str, _ServerEntry] = {
            name: _ServerEntry(name=name, url=url) for name, url in servers.items()
        }
        self._lock = asyncio.Lock()
        self._hidden: set = hidden_tools or set()

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        await asyncio.gather(*(self._connect(e) for e in self._servers.values()))

    async def shutdown(self) -> None:
        for entry in self._servers.values():
            await self._close(entry)

    async def _connect(self, entry: _ServerEntry) -> None:
        entry.connected = False
        entry.error = None
        stack = AsyncExitStack()
        try:
            streams = await stack.enter_async_context(sse_client(entry.url))
            session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
            await asyncio.wait_for(session.initialize(), timeout=15.0)
            tools_resp = await asyncio.wait_for(session.list_tools(), timeout=15.0)
            entry.session = session
            entry.stack = stack
            entry.tools = list(tools_resp.tools)
            entry.connected = True
            logger.info("MCP[%s] connected, %d tools", entry.name, len(entry.tools))
        except Exception as exc:
            entry.error = f"{type(exc).__name__}: {exc}"
            logger.warning("MCP[%s] connect failed: %s", entry.name, entry.error)
            try:
                await stack.aclose()
            except Exception:
                pass
            entry.session = None
            entry.stack = None
            entry.tools = []

    async def _close(self, entry: _ServerEntry) -> None:
        if entry.stack is not None:
            try:
                await entry.stack.aclose()
            except Exception as exc:
                logger.warning("MCP[%s] close error: %s", entry.name, exc)
        entry.session = None
        entry.stack = None
        entry.connected = False

    # ── introspection ────────────────────────────────────────────────────────

    def status(self) -> list[dict]:
        return [
            {
                "name": e.name,
                "url": e.url,
                "connected": e.connected,
                "tool_count": len(e.tools),
                "error": e.error,
            }
            for e in self._servers.values()
        ]

    def all_tools_as_openai_schema(self) -> list[dict]:
        out: list[dict] = []
        for entry in self._servers.values():
            if not entry.connected:
                continue
            for t in entry.tools:
                qname = f"{entry.name}_{t.name}"
                if qname in self._hidden:
                    continue
                out.append({
                    "type": "function",
                    "function": {
                        "name": qname,
                        "description": t.description or "",
                        "parameters": t.inputSchema or {"type": "object", "properties": {}},
                    },
                })
        # ── synthetic meta-tool: fan-out search across all sources ───────────
        sources = sorted([
            s for s in ("email", "slack", "clickup", "granola", "onenote")
            if s in self._servers and self._servers[s].connected
        ])
        out.append({
            "type": "function",
            "function": {
                "name": "multi_search",
                "description": (
                    "Search ALL sources at once for a single query, server-side parallel. "
                    "Use this for any cross-source question (e.g. 'find everything about X'). "
                    "Returns a section per source with top results. "
                    "Prefer over calling per-source search tools sequentially."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query terms"},
                        "sources": {
                            "type": "array",
                            "items": {"type": "string", "enum": sources},
                            "description": f"Optional list to restrict; default = all of {sources}",
                        },
                        "per_source_limit": {"type": "integer", "default": 10,
                            "description": "Max results per source (1-25)"},
                    },
                    "required": ["query"],
                },
            },
        })
        out.append({
            "type": "function",
            "function": {
                "name": "priority_digest",
                "description": (
                    "Return a cross-source digest of likely priorities: open ClickUp tasks (most-recently-updated first), "
                    "recent unread emails from real humans, recent Slack DMs/MPIMs from teammates with question marks "
                    "(proxy for asks), and recent meetings. Use this for questions like 'what should I work on', "
                    "'what are my priorities', 'what is on my plate', 'what is urgent', 'triage my inbox', "
                    "'summarize my agenda', or any other broad prioritization / agenda / triage question. "
                    "Returns a single structured markdown digest in ONE call — do NOT call multi_search for these."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days_email": {"type": "integer", "default": 7,
                            "description": "Window for unread emails (days)"},
                        "days_slack": {"type": "integer", "default": 3,
                            "description": "Window for recent Slack messages from teammates (days)"},
                        "days_meetings": {"type": "integer", "default": 14,
                            "description": "Window for recent meetings (days)"},
                        "top_tasks": {"type": "integer", "default": 20,
                            "description": "How many open tasks to surface (newest-updated first)"}
                    },
                    "required": []
                },
            },
        })
        return out

    def tool_list(self) -> list[dict]:
        out: list[dict] = []
        for entry in self._servers.values():
            for t in entry.tools:
                out.append({
                    "server": entry.name,
                    "name": t.name,
                    "qualified_name": f"{entry.name}_{t.name}",
                    "description": t.description or "",
                })
        return out

    # ── invocation ───────────────────────────────────────────────────────────

    def _split_qname(self, qname: str) -> tuple[str, str] | None:
        # Split on first underscore; server names in config are single-word.
        for server_name in self._servers.keys():
            prefix = server_name + "_"
            if qname.startswith(prefix):
                return server_name, qname[len(prefix):]
        # Fallback to first underscore split
        if "_" in qname:
            a, b = qname.split("_", 1)
            return a, b
        return None

    async def call(self, qualified_name: str, arguments: dict) -> str:
        if qualified_name == "multi_search":
            return await self._multi_search(arguments)
        if qualified_name == "priority_digest":
            return await self._priority_digest(arguments)
        split = self._split_qname(qualified_name)
        if split is None:
            return f"[error] cannot route tool name: {qualified_name}"
        server_name, tool_name = split
        entry = self._servers.get(server_name)
        if entry is None:
            return f"[error] unknown server: {server_name}"

        try:
            return await self._invoke(entry, tool_name, arguments)
        except Exception as exc:
            logger.warning("MCP[%s].%s call failed (%s); attempting reconnect",
                           server_name, tool_name, exc)
            async with self._lock:
                await self._close(entry)
                await self._connect(entry)
            if not entry.connected:
                return f"[error] {server_name} disconnected: {entry.error}"
            try:
                return await self._invoke(entry, tool_name, arguments)
            except Exception as exc2:
                logger.error("MCP[%s].%s retry failed: %s", server_name, tool_name, exc2)
                return f"[error] {server_name}_{tool_name} failed after reconnect: {exc2}"

    async def _invoke(self, entry: _ServerEntry, tool_name: str, arguments: dict) -> str:
        if not entry.session:
            raise RuntimeError("session not initialised")
        resp = await asyncio.wait_for(
            entry.session.call_tool(tool_name, arguments or {}),
            timeout=120.0,
        )
        # Join text content
        parts: list[str] = []
        for c in resp.content or []:
            text = getattr(c, "text", None)
            if text:
                parts.append(text)
        out = "\n".join(parts) if parts else "(no text content)"
        if getattr(resp, "isError", False):
            return f"[tool error] {out}"
        return out


    async def _multi_search(self, arguments: dict) -> str:
        """Fan-out search across all 5 sources in parallel."""
        query = arguments.get("query", "")
        if not query:
            return "Error: query is required"
        per_limit = int(arguments.get("per_source_limit", 10))
        per_limit = max(1, min(per_limit, 25))
        requested = set(arguments.get("sources") or [])

        plan = [
            ("email",   "search_emails",   {"query": query, "limit": per_limit}),
            ("slack",   "search_messages", {"query": query, "limit": per_limit}),
            ("clickup", "search_tasks",    {"query": query, "limit": per_limit}),
            ("granola", "search_notes",    {"query": query, "limit": per_limit}),
            ("onenote", "search_pages",    {"query": query, "limit": per_limit}),
        ]
        plan = [p for p in plan if (not requested or p[0] in requested)
                and p[0] in self._servers and self._servers[p[0]].connected]

        async def _one(source: str, tool: str, args: dict) -> tuple[str, str]:
            try:
                entry = self._servers[source]
                text = await self._invoke(entry, tool, args)
                return source, text
            except Exception as exc:
                return source, f"(error: {exc})"

        results = await asyncio.gather(*(_one(*p) for p in plan))
        chunks = []
        for source, text in results:
            chunks.append(f"=== {source.upper()} ===\n{text.strip() or "(no results)"}\n")
        return "\n".join(chunks)


    async def _priority_digest(self, arguments: dict) -> str:
        """Cross-source digest for prioritization / triage / agenda questions."""
        import sqlite3
        from pathlib import Path

        days_email = int(arguments.get("days_email", 7))
        days_slack = int(arguments.get("days_slack", 3))
        days_meetings = int(arguments.get("days_meetings", 14))
        top_tasks = int(arguments.get("top_tasks", 20))

        home = Path.home()
        paths = {
            "clickup": home / ".clickup-mcp" / "clickup.db",
            "email":   home / ".email-mcp" / "mail.db",
            "slack":   home / ".slack-mcp" / "slack.db",
            "granola": home / ".granola-mcp" / "granola.db",
        }

        def _query(db_path, sql, params=()):
            if not db_path.exists():
                return []
            conn = sqlite3.connect(str(db_path))
            try:
                return conn.execute(sql, params).fetchall()
            finally:
                conn.close()

        loop = asyncio.get_running_loop()

        async def _tasks():
            return await loop.run_in_executor(None, _query, paths["clickup"], """
                SELECT name,
                       date(CAST(date_updated AS INTEGER)/1000,'unixepoch') AS upd,
                       status,
                       CASE WHEN due_date IS NOT NULL AND due_date != ''
                            THEN date(CAST(due_date AS INTEGER)/1000,'unixepoch') ELSE '' END AS due
                FROM tasks
                WHERE is_closed = 0
                ORDER BY CAST(date_updated AS INTEGER) DESC
                LIMIT ?
                """, (top_tasks,))

        async def _emails():
            return await loop.run_in_executor(None, _query, paths["email"], """
                SELECT date(received_datetime) AS d,
                       COALESCE(NULLIF(sender_name,''), sender_email) AS who,
                       sender_email,
                       substr(subject, 1, 100) AS subj
                FROM messages
                WHERE is_read = 0
                  AND date(received_datetime) >= date('now', ?)
                  AND sender_email NOT LIKE '%no-reply%'
                  AND sender_email NOT LIKE '%noreply%'
                  AND sender_email NOT LIKE '%notifications@%'
                  AND sender_email NOT LIKE 'root@%'
                  AND sender_email NOT LIKE '%login%'
                  AND sender_email NOT LIKE '%flowserver%'
                  AND sender_email NOT LIKE 'wiki@%'
                  AND sender_email NOT LIKE 'help+jira%'
                ORDER BY received_datetime DESC
                LIMIT 30
                """, (f"-{days_email} days",))

        async def _slack():
            return await loop.run_in_executor(None, _query, paths["slack"], """
                SELECT datetime(CAST(m.ts AS REAL),'unixepoch') AS when_,
                       COALESCE(NULLIF(u.real_name,''), u.name) AS who,
                       c.type AS ctype,
                       COALESCE(c.name, '') AS cname,
                       substr(REPLACE(m.text, char(10), ' '), 1, 140) AS text
                FROM messages m
                JOIN users u ON u.id = m.user_id
                JOIN conversations c ON c.id = m.channel_id
                WHERE u.is_bot = 0
                  AND u.is_deleted = 0
                  AND u.name != 'cheller'
                  AND CAST(m.ts AS REAL) >= strftime('%s','now', ?)
                  AND (m.text LIKE '%?%' OR c.type IN ('im','mpim'))
                ORDER BY CAST(m.ts AS REAL) DESC
                LIMIT 30
                """, (f"-{days_slack} days",))

        async def _meetings():
            return await loop.run_in_executor(None, _query, paths["granola"], """
                SELECT date(created_at) AS d,
                       substr(COALESCE(NULLIF(title,''), calendar_event_title, '(untitled)'), 1, 110) AS title
                FROM notes
                WHERE date(created_at) >= date('now', ?)
                ORDER BY created_at DESC
                LIMIT 25
                """, (f"-{days_meetings} days",))

        tasks_r, emails_r, slack_r, meetings_r = await asyncio.gather(
            _tasks(), _emails(), _slack(), _meetings()
        )

        out = ["# Priority digest",
               "_Cross-source pull. Synthesise priorities by looking for items appearing in multiple lists._",
               "",
               f"## Open ClickUp tasks ({len(tasks_r)}, newest-updated first)"]
        if not tasks_r:
            out.append("_(none)_")
        for name, upd, status, due in tasks_r:
            tail = f"  due {due}" if due else ""
            out.append(f"- **{name}**  _(status: {status}, updated {upd}{tail})_")

        out += ["", f"## Unread emails from humans, last {days_email} days ({len(emails_r)})"]
        if not emails_r:
            out.append("_(none)_")
        for d, who, addr, subj in emails_r:
            out.append(f"- `{d}` **{who}** — {subj}")

        out += ["", f"## Recent Slack from teammates (DMs/MPIMs or messages with '?'), last {days_slack} days ({len(slack_r)})"]
        if not slack_r:
            out.append("_(none)_")
        for when_, who, ctype, cname, text in slack_r:
            loc = "DM" if ctype == "im" else ("MPIM" if ctype == "mpim" else f"#{cname}")
            out.append(f"- `{when_}` **{who}** [{loc}] — {text}")

        out += ["", f"## Meetings last {days_meetings} days ({len(meetings_r)})"]
        if not meetings_r:
            out.append("_(none)_")
        for d, title in meetings_r:
            out.append(f"- `{d}` {title}")

        return "\n".join(out)
