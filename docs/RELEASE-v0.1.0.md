# v0.1.0 — Initial public release

First public version of a local-first personal-knowledge MCP stack.

## What's in

- **Five sync MCPs** (`email`, `slack`, `clickup`, `granola`, `onenote`) — each
  mirrors its upstream API into a local SQLite cache with FTS5 search,
  serves search/get/list/count tools over MCP-SSE, and re-syncs on a
  10–30 min schedule.
- **One chat web UI** (`chat-mcp`) — FastAPI + vanilla HTML, connects to all
  sync MCPs as a client, auto-discovers their tools, and forwards to a local
  LLM via OpenAI-compatible chat completions. Adds two synthetic meta-tools
  (`multi_search`, `priority_digest`) for cross-source queries.
- **47 LLM-visible tools** across the 5 sources + 2 synthetic.
- **systemd units** for all 6 services; auto-restart, linger-aware.
- **Top-level scripts**: `bootstrap.sh` (guided install), `health_check.sh`
  (cross-service freshness audit).

## Documentation

- `README.md` — overview and quick start
- `SETUP.md` — step-by-step replication walkthrough (per-source token setup)
- `OPERATIONS.md` — day-to-day operations, ports, logs, and the known-issues
  catalog (14 gotchas documented)
- `WRITEUP.md` — long-form essay on motivation, design, lessons learned
- `PITCH.md` — one-page summary for talks or proposals
- `CONTRIBUTING.md` — adding new source MCPs

## Tested with

- Ubuntu 24.04 on ARM (Nvidia GB10) and x86
- Python 3.12
- Ollama with `qwen3:32b` (≈8s warm tool-call latency)
- Microsoft 365 / Graph API (delegated permissions, device-code flow)
- Slack User OAuth tokens
- ClickUp Personal API tokens
- Granola Enterprise API keys

## Known limits (documented in OPERATIONS.md)

- OneNote's per-tenant 5,000-OneNote-item SharePoint ceiling caps some
  legacy content from being enumerated. Workaround in place (flat
  endpoint), gracefully skips affected sections.
- `qwen3-coder:30b` crashes the Ollama runner on Grace-Blackwell ARM.
  `qwen3:32b` works fine; `nemotron-3-super:120b-a12b` works but is slow.
- The chat UI auth is a deliberate stub; deploy on a trusted network
  (LAN + Tailscale) or wire a real auth dependency in
  `chat-mcp/chat/auth.py`.
- No tests. PRs welcome.

## License

MIT.
