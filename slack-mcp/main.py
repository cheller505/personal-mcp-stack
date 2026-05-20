#!/usr/bin/env python3
"""Slack MCP server entry point."""

import asyncio
import logging
import os
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".slack-mcp"
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

from slack_mcp.auth import ensure_authenticated, get_token
from slack_mcp import database as db
from slack_mcp import sync
from slack_mcp.server import run_server


async def main() -> None:
    # ── Auth ──────────────────────────────────────────────────────────────────
    print("Checking Slack authentication...", flush=True)
    ensure_authenticated()
    print("", flush=True)

    # ── Database ──────────────────────────────────────────────────────────────
    db.init_db()

    # ── Initial sync ──────────────────────────────────────────────────────────
    if not db.get_sync_state("full_sync_complete"):
        await sync.run_full_sync(get_token)
    else:
        last_full = db.get_sync_state("last_full_sync")
        print(f"Existing cache found (last full sync: {last_full})")
        print("Running delta sync to catch up...\n", flush=True)
        changes = await sync.run_delta_sync(get_token)
        if changes:
            print(f"Delta sync: {changes} new messages.\n", flush=True)
        else:
            print("Delta sync: already up to date.\n", flush=True)

    # ── Background scheduler ──────────────────────────────────────────────────
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        sync.run_delta_sync,
        trigger="interval",
        args=[get_token],
        minutes=15,
        id="delta_sync",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        sync.run_channel_resync,
        trigger="interval",
        args=[get_token],
        minutes=30,
        id="channel_resync",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    print("Background delta sync every 5 min; channel resync every 30.\n", flush=True)

    # ── MCP server ────────────────────────────────────────────────────────────
    host = os.environ.get("SLACK_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("SLACK_MCP_PORT", "8770"))

    print("=" * 56)
    print(f"  Slack MCP server ready")
    print(f"  SSE endpoint: http://{host}:{port}/sse")
    print("=" * 56, flush=True)

    await run_server(get_token, host=host, port=port)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.", flush=True)
