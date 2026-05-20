#!/usr/bin/env python3
"""chat-mcp entry point."""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn

from chat.config import CONFIG_DIR, Config
from chat.llm import LLMClient
from chat.mcp_pool import MCPPool
from chat.server import build_app


def _setup_logging() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(CONFIG_DIR / "chat.log"),
        ],
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)


async def main() -> None:
    _setup_logging()
    log = logging.getLogger("chat-mcp")

    cfg = Config.load()
    log.info("Loaded config: model=%s, %d MCP servers", cfg.lumen.model, len(cfg.mcp_servers))

    pool = MCPPool(cfg.mcp_servers, hidden_tools=cfg.hidden_tools)
    await pool.startup()

    llm = LLMClient(cfg.lumen)

    app = build_app(cfg, pool, llm)

    # Wire shutdown via FastAPI lifespan in uvicorn — we manage manually since
    # the pool/llm are already constructed.
    config = uvicorn.Config(
        app, host=cfg.bind.host, port=cfg.bind.port, log_level="info",
        loop="asyncio", lifespan="off",
    )
    server = uvicorn.Server(config)

    log.info("Serving http://%s:%d", cfg.bind.host, cfg.bind.port)
    try:
        await server.serve()
    finally:
        log.info("Shutting down — closing MCP pool + LLM client")
        await pool.shutdown()
        await llm.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.", flush=True)
