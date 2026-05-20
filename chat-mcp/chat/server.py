"""FastAPI app: routes, SSE chat streaming, in-memory session store."""

import json
import logging
import uuid
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .auth import make_dependency
from .config import Config
from .llm import LLMClient
from .mcp_pool import MCPPool
from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)

UI_HTML_PATH = Path(__file__).parent / "ui.html"


def build_app(cfg: Config, pool: MCPPool, llm: LLMClient) -> FastAPI:
    app = FastAPI(title="chat-mcp")
    require_user = make_dependency(cfg)
    orch = Orchestrator(llm, pool)

    # In-memory session store (single user — restart loses history)
    sessions: dict[str, list[dict]] = {}

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def index(_user: str = Depends(require_user)):
        return HTMLResponse(UI_HTML_PATH.read_text())

    @app.get("/api/tools")
    async def list_tools(_user: str = Depends(require_user)):
        return {"tools": pool.tool_list()}

    @app.get("/api/mcp_status")
    async def mcp_status(_user: str = Depends(require_user)):
        return {
            "model": cfg.lumen.model,
            "servers": pool.status(),
        }

    @app.post("/api/chat")
    async def chat(request: Request, _user: str = Depends(require_user)):
        body = await request.json()
        user_text = (body.get("message") or "").strip()
        session_id = body.get("session_id") or str(uuid.uuid4())
        if not user_text:
            raise HTTPException(status_code=400, detail="empty message")

        history = sessions.setdefault(session_id, [])
        messages = orch.build_messages(history, user_text)

        async def event_stream() -> AsyncIterator[bytes]:
            yield _sse({"type": "session", "session_id": session_id})
            try:
                async for evt in orch.run(messages):
                    yield _sse(evt)
            except Exception as exc:
                logger.exception("chat stream error")
                yield _sse({"type": "error", "message": str(exc)})
                yield _sse({"type": "done"})
                return
            # Persist updated messages (strip system prompt — it's re-added each turn)
            sessions[session_id] = [m for m in messages if m.get("role") != "system"]

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/reset")
    async def reset(request: Request, _user: str = Depends(require_user)):
        body = await request.json()
        sid = body.get("session_id")
        if sid and sid in sessions:
            del sessions[sid]
        return JSONResponse({"ok": True})

    return app


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")
