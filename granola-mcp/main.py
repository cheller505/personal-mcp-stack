#!/usr/bin/env python3
"""Granola MCP server entry point."""

import asyncio
import logging
import os
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".granola-mcp"
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

from granola_mcp.auth import ensure_authenticated, get_api_key
from granola_mcp import database as db
from granola_mcp import sync
from granola_mcp.server import run_server


async def main() -> None:
    # ── Auth ──────────────────────────────────────────────────────────────────
    print("Checking Granola authentication...", flush=True)
    ensure_authenticated()
    print("", flush=True)

    # ── Database ──────────────────────────────────────────────────────────────
    db.init_db()

    # ── Initial sync ──────────────────────────────────────────────────────────
    full_complete = db.get_sync_state("full_sync_complete") == "1"
    if not full_complete:
        await sync.run_full_sync(get_api_key)
    else:
        last_full = db.get_sync_state("last_full_sync")
        print(f"Existing cache found (last full sync: {last_full})")
        print("Running incremental sync to catch up...\n", flush=True)
        changes = await sync.run_incremental_sync(get_api_key)
        if changes:
            print(f"Incremental sync: {changes} changes applied.\n", flush=True)
        else:
            print("Incremental sync: already up to date.\n", flush=True)

    # ── Background scheduler ──────────────────────────────────────────────────
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        sync.run_incremental_sync,
        trigger="interval",
        args=[get_api_key],
        minutes=30,
        id="incremental_sync",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        sync.retry_pending,
        trigger="interval",
        args=[get_api_key],
        minutes=60,
        id="retry_pending",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    print("Background: incremental sync every 30 min, pending retry every 60 min.\n", flush=True)

    # ── MCP server ────────────────────────────────────────────────────────────
    host = os.environ.get("GRANOLA_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("GRANOLA_MCP_PORT", "8768"))

    print("=" * 56)
    print(f"  Granola MCP server ready")
    print(f"  SSE endpoint: http://{host}:{port}/sse")
    print("=" * 56, flush=True)

    await run_server(get_api_key, host=host, port=port)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.", flush=True)
