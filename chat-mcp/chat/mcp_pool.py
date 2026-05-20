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
