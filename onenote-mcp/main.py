#!/usr/bin/env python3
"""OneNote MCP server entry point."""

import asyncio
import logging
import os
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".onenote-mcp"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(CONFIG_DIR / "sync.log"),
    ],
)

# Silence noisy libs
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

from onenote_mcp.auth import ensure_authenticated, get_fresh_token
from onenote_mcp import database as db
from onenote_mcp import sync
from onenote_mcp.server import run_server


async def main() -> None:
    # ── Auth ──────────────────────────────────────────────────────────────────
    print("Checking authentication...", flush=True)
    cache, _token = ensure_authenticated()
    print("Authenticated.\n", flush=True)

    def get_token() -> str:
        return get_fresh_token(cache)

    # ── Database ──────────────────────────────────────────────────────────────
    db.init_db()

    # ── Initial sync ──────────────────────────────────────────────────────────
    last_full = db.get_sync_state("last_full_sync")
    if not last_full:
        await sync.run_full_sync(get_token)
    else:
        print(f"Existing cache found (last full sync: {last_full})")
        print("Running delta sync to catch up...\n", flush=True)
        changes = await sync.run_delta_sync(get_token)
        if changes:
            print(f"Delta sync: {changes} changes applied.\n", flush=True)
        else:
            print("Delta sync: already up to date.\n", flush=True)

    # ── Background scheduler ──────────────────────────────────────────────────
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        sync.run_delta_sync,
        trigger="interval",
        args=[get_token],
        minutes=30,
        id="delta_sync",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        sync.run_structure_sync,
        trigger="interval",
        args=[get_token],
        hours=4,
        id="structure_sync",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    print(
        "Background delta sync scheduled every 30 minutes; "
        "structure re-enum every 4 hours.\n",
        flush=True,
    )

    # ── MCP server ────────────────────────────────────────────────────────────
    host = os.environ.get("ONENOTE_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("ONENOTE_MCP_PORT", "8769"))

    print("=" * 56)
    print(f"  OneNote MCP server ready")
    print(f"  SSE endpoint: http://{host}:{port}/sse")
    print("=" * 56, flush=True)

    await run_server(get_token, host=host, port=port)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.", flush=True)
