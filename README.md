# Personal Knowledge MCP Stack

Six locally-hosted services that mirror your email, Slack, task tracker, AI
meeting notes, and notebook/wiki pages into local SQLite caches, serve them via
the Model Context Protocol (MCP), and front them with a self-hosted chat web
UI backed by a local LLM. Everything runs on one machine. No data leaves your
network.

## What's in the box

```
┌─ your machine ──────────────────────────────────────────────────────┐
│                                                                     │
│  Chat UI ──→ Local LLM (Ollama)                                     │
│      │                                                              │
│      ├──→ email-mcp    →  SQLite cache + FTS5  (Microsoft Graph)    │
│      ├──→ slack-mcp    →  SQLite cache + FTS5  (Slack Web API)      │
│      ├──→ clickup-mcp  →  SQLite cache + FTS5  (ClickUp REST)       │
│      ├──→ granola-mcp  →  SQLite cache + FTS5  (Granola Enterprise) │
│      └──→ onenote-mcp  →  SQLite cache + FTS5  (Microsoft Graph)    │
│                                                                     │
│  Each sync-MCP polls its upstream API on a 10–30 min schedule       │
│  (delta/incremental), serves search/get/count tools over MCP-SSE.   │
│  Chat UI connects as an MCP client, exposes all tools to the LLM,   │
│  plus synthetic meta-tools for cross-source search & priority       │
│  digests.                                                           │
└─────────────────────────────────────────────────────────────────────┘
```

## Why this exists

- **Privacy.** Your work data never leaves your network. The LLM is local.
- **Speed.** Local FTS5 search across all sources is sub-second; you don't
  pay an API round-trip per query.
- **Continuity.** Search and chat over your full history (years of email,
  thousands of Slack messages) even when upstream APIs are down or slow.
- **Composability.** Each source is a vanilla MCP server, so any MCP client
  (Claude Desktop, Claude Code, your own bot) can use the same data. The chat
  UI is just one consumer.

## What it doesn't do

- **No vector/semantic search** — just FTS5 keyword + filters. Add an embedding
  layer if you need it.
- **No write-heavy workflows** — limited write tools (draft email, create task,
  post comment) exist but read is the focus.
- **No multi-user.** Single operator, single LLM, single chat session at a
  time. The auth layer is a stub.

## Quick start

### Option A — let Claude Code set it up for you

If you have [Claude Code](https://claude.ai/code) (or Codex, Aider, or any
other agentic coding tool that can read a repo's `CLAUDE.md`) installed on
the target host, you can paste this prompt and let it drive the install
interactively:

> Set up the personal-mcp-stack project on this machine. Clone
> `https://github.com/cheller505/personal-mcp-stack.git` into `~/projects/`,
> then read `~/projects/CLAUDE.md` and follow it. Walk me through each
> credential I need to provide; do not invent values. Use the existing
> `bootstrap.sh` and `health_check.sh` scripts as documented.

`CLAUDE.md` is the agent-facing reference; it covers prerequisite checks,
install order, per-source credential prompts, common failure modes, and
verification steps.

### Option B — manual install


You need:
- Linux host (tested on Ubuntu 24.04 on ARM/GB10; should work on x86)
- Python 3.11+
- ~5GB free for the email cache (depending on mailbox size)
- An LLM endpoint — either local (Ollama recommended) or remote OpenAI-compatible
- Whatever upstream credentials apply (Microsoft Graph, Slack token, etc.)

```bash
git clone <this-repo>.git ~/projects
cd ~/projects
./bootstrap.sh           # installs the sources you pick + the chat UI
./health_check.sh        # verify everything's syncing
```

See **`SETUP.md`** for the full step-by-step (token generation, Azure app
registration, per-source quirks).

## Documentation map

| File | What it is |
|---|---|
| `README.md` | This file — overview + quick start |
| `SETUP.md` | Step-by-step replication guide (token generation, install order, troubleshooting) |
| `OPERATIONS.md` | Day-to-day operations: service control, log paths, health checks, the full known-issues catalog |
| `WRITEUP.md` | Long-form essay on why I built this, design decisions, lessons learned, gotchas |
| `PITCH.md` | One-page summary suitable for a lightning talk or proposal abstract |
| `CONTRIBUTING.md` | If you want to add another data source or tool |
| `LICENSE` | MIT |

## Repository layout

```
projects/
├── email-mcp/        # Mail sync (Microsoft Graph, MSAL device-code auth)
├── slack-mcp/        # Slack sync (User OAuth token)
├── clickup-mcp/      # ClickUp sync (Personal API token)
├── granola-mcp/      # Granola meeting notes (Enterprise API key)
├── onenote-mcp/      # OneNote pages (Microsoft Graph, same Azure app as email)
├── chat-mcp/         # FastAPI web UI + LLM orchestrator
├── bootstrap.sh      # Top-level installer
├── health_check.sh   # Cross-service freshness audit
└── README.md         # ← you are here
```

Each `<service>-mcp/` follows the same shape:
- `<package>/auth.py` — credential handling, token storage at `~/.<service>-mcp/`
- `<package>/database.py` — SQLite schema + FTS5 + indexes
- `<package>/api.py` or `graph.py` — async HTTP client with rate limiting
- `<package>/sync.py` — full sync (resumable) + delta/incremental sync
- `<package>/tools.py` — MCP tool definitions (search, get, list, count, …)
- `<package>/server.py` — MCP SSE server (raw ASGI app, not Starlette routes — see WRITEUP for why)
- `main.py` — entry point with APScheduler-driven background sync
- `install.sh` — venv + systemd unit setup
- `<service>-mcp.service` — systemd user unit

## Status

This is a personal project, not a product. It works for me. It might work for
you. PRs and forks welcome. See `CONTRIBUTING.md` if you want to add a source.

License: MIT.
