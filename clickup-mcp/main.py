#!/usr/bin/env python3
"""ClickUp MCP server entry point."""

import asyncio
import logging
import os
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".clickup-mcp"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(CONFIG_DIR / "sync.log"),
    ],
)

logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

from clickup_mcp.auth import ensure_authenticated, get_token
from clickup_mcp import database as db
from clickup_mcp import sync
from clickup_mcp.server import run_server


async def main() -> None:
    # ── Auth ──────────────────────────────────────────────────────────────────
    print("Checking ClickUp authentication...", flush=True)
    ensure_authenticated()
    print("", flush=True)

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
        minutes=10,
        id="delta_sync",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        sync.run_member_sync,
        trigger="interval",
        args=[get_token],
        minutes=60,
        id="member_sync",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    print("Background delta sync scheduled every 10 minutes; members every 60.\n", flush=True)

    # ── MCP server ────────────────────────────────────────────────────────────
    host = os.environ.get("EMAIL_MCP_HOST") or os.environ.get("CLICKUP_MCP_HOST") or "127.0.0.1"
    port = int(os.environ.get("CLICKUP_MCP_PORT", "8767"))

    print("=" * 56)
    print(f"  ClickUp MCP server ready")
    print(f"  SSE endpoint: http://{host}:{port}/sse")
    print("=" * 56, flush=True)

    await run_server(get_token, host=host, port=port)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.", flush=True)
