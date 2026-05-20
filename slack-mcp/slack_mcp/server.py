"""MCP server with SSE transport (HTTP, not stdio).

Uses a plain ASGI app to avoid Starlette version compatibility issues —
SseServerTransport.connect_sse() and handle_post_message() are ASGI callables
that expect raw (scope, receive, send) directly.
"""

import logging
from typing import Callable

import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport

from .tools import get_tools, handle_tool

logger = logging.getLogger(__name__)


def build_mcp_server(get_token: Callable[[], str]) -> Server:
    server = Server("slack-mcp")

    @server.list_tools()
    async def _list_tools():
        return get_tools()

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict):
        return await handle_tool(name, arguments, get_token)

    return server


class _SlackMCPApp:
    """Minimal ASGI router — avoids Starlette Request._send which was removed in 1.0."""

    def __init__(self, sse: SseServerTransport, mcp_server: Server) -> None:
        self._sse = sse
        self._mcp = mcp_server

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await _handle_lifespan(receive, send)
            return

        if scope["type"] != "http":
            return

        path = scope.get("path", "")

        if path == "/sse":
            async with self._sse.connect_sse(scope, receive, send) as streams:
                await self._mcp.run(
                    streams[0],
                    streams[1],
                    self._mcp.create_initialization_options(),
                )

        elif path == "/messages":
            await self._sse.handle_post_message(scope, receive, send)

        else:
            body = b"Slack MCP server. Connect via /sse"
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [[b"content-type", b"text/plain"]],
            })
            await send({"type": "http.response.body", "body": body})


async def _handle_lifespan(receive, send) -> None:
    while True:
        event = await receive()
        if event["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif event["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


async def run_server(
    get_token: Callable[[], str],
    host: str = "127.0.0.1",
    port: int = 8770,
) -> None:
    mcp_server = build_mcp_server(get_token)
    sse = SseServerTransport("/messages")
    app = _SlackMCPApp(sse, mcp_server)

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    logger.info("MCP server listening on http://%s:%d/sse", host, port)
    await server.serve()
