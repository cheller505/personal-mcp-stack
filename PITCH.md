# Personal Knowledge MCP Stack — one-page summary

A self-hosted, local-first system that mirrors all my work data sources
(email, Slack, task tracker, AI meeting notes, notebook pages) into local
SQLite caches with full-text search, and serves them to a local LLM through
the Model Context Protocol. Built in 36 hours; running in production on a
single GPU host. MIT licensed.

## What problem it solves

Work data lives in 5+ silos with no cross-search. Hosted LLM connectors exist
but send query traffic off-host and don't keep a local archive. I wanted
a private, fast, full-history mirror that any MCP-aware client (chat UI,
Claude Desktop, Claude Code, custom bots) can query.

## Architecture (60 seconds)

```
Chat UI → Local LLM (Ollama)
   ↓
   ├── email-mcp    → SQLite + FTS5  (Microsoft Graph)
   ├── slack-mcp    → SQLite + FTS5  (Slack Web API)
   ├── clickup-mcp  → SQLite + FTS5  (ClickUp REST)
   ├── granola-mcp  → SQLite + FTS5  (Granola Enterprise)
   └── onenote-mcp  → SQLite + FTS5  (Microsoft Graph)
```

Each source is an independent MCP server that polls upstream on a schedule
(10–30 min delta sync) and serves search/get/count tools over MCP-SSE. The
chat UI is itself an MCP client, exposing the union of all source tools to
the LLM plus two synthetic meta-tools (cross-source keyword search;
cross-source priority digest). Six systemd services, ~3,000 LOC total.

## Why it matters

- **Privacy by default.** No data leaves the host. The LLM is local.
- **Sub-second search** across years of history with SQLite FTS5.
- **Composable.** Any MCP client (Claude Desktop / Code, custom bots, future
  agents) can use the same tools — the chat UI is just one consumer.
- **Forkable per-source pattern.** Each MCP is a 200-line skeleton;
  adding a new source (say Confluence or Jira) is hours, not weeks.

## What I learned

- MCP SDK examples assume Starlette internals that broke in 1.0 — raw ASGI
  apps sidestep this and are about 60 lines.
- LLM tool-surface bloat (55 tools = 8K tokens of overhead) measurably
  hurts tool-selection accuracy on small/medium models. Hiding admin tools
  and adding synthetic meta-tools recovers that.
- "How many X" questions devolve into pagination loops without dedicated
  `count_*` tools that do server-side aggregation.
- Pick LLMs by hardware compatibility, not benchmark numbers — `qwen3-coder:30b`
  crashes on ARM/MoE runners; `qwen3:32b` is the same size and works fine.
- Delta-sync state advancement is invisible if you don't check it. A
  case-sensitive query parameter (`$deltaToken` vs `$deltatoken`) had me
  re-syncing 9,688 messages every cycle for 18 hours before I noticed.

## Recipe

1. Each source ≈ one MCP server with a fixed skeleton (`auth`, `database`,
   `api`, `sync`, `tools`, `server`, `main`). The shape is mechanical;
   substitute upstream-API specifics.
2. Local SQLite + FTS5 per source. Trade some duplication for schema
   independence — you can rip out a source without touching the others.
3. Chat UI is an MCP client (not server) with `asyncio.gather` over tool
   calls. Stream chunks back over SSE so the user sees progress.
4. Hide diagnostic tools, add synthetic meta-tools, document the gotchas
   in your repo so the next person doesn't rediscover them.

## Status & artifacts

- Working on one host today, ~300k records cached across 5 sources
- Source + docs: see repo (this file lives next to a long-form WRITEUP.md
  and a step-by-step SETUP.md)
- 36 hours' build time from scratch; 5 sources synced cleanly, 47 LLM-visible
  tools + 2 meta-tools, sub-second chat responses on local hardware
- MIT licensed; PRs/forks welcome, especially new source MCPs
