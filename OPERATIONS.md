# Memex — operations

Six locally-hosted services that mirror cheller's email, Slack, ClickUp,
Granola meeting notes, and OneNote into local SQLite caches, serve them via
the Model Context Protocol (MCP) over SSE, and front them with a small chat
web UI backed by a local LLM running on the same host.

**Built:** 2026-05-19 → 2026-05-20.  **Host:** `<runtime-host>` (the operator's organisation, ARM/GB10).
**Access:** LAN + Tailscale only. No public endpoint.

---

## TL;DR

- All services run on **<runtime-host>** as systemd user services.
- Chat UI is at **`http://<runtime-host>:8082/`** (also `http://<runtime-host>.<your-tailnet>.ts.net:8082/` via Tailscale magicDNS).
- Sync MCPs listen on `127.0.0.1:876{5,7,8,9}` and `:8770`. Chat UI binds `0.0.0.0:8082`.
- LLM = local **Ollama** at `127.0.0.1:11434`, model **`qwen3:32b`** (kept warm for 2h between requests).
- All authentication tokens live in `~/.{service}-mcp/{token_cache.json|config.json}` (chmod 600).
- Original setup ran on **<source-host>**; data was migrated to <runtime-host> on 2026-05-20. Warspite services
  are disabled but data dirs are preserved as a backup.

---

## Architecture

```
┌─────────────────────── <runtime-host> (the operator's organisation, ARM/GB10) ─────────────────────┐
│                                                                       │
│  Browser (LAN or Tailscale)                                           │
│         │                                                             │
│         ▼                                                             │
│  chat-mcp :8082  ── FastAPI + vanilla HTML chat UI                    │
│         │                                                             │
│         ├──→  Ollama :11434  ── qwen3:32b (OpenAI-compatible API)     │
│         │                                                             │
│         ├──→  email-mcp   :8765  ──→ SQLite ~/.email-mcp/mail.db      │
│         ├──→  clickup-mcp :8767  ──→ SQLite ~/.clickup-mcp/clickup.db │
│         ├──→  granola-mcp :8768  ──→ SQLite ~/.granola-mcp/granola.db │
│         ├──→  onenote-mcp :8769  ──→ SQLite ~/.onenote-mcp/onenote.db │
│         └──→  slack-mcp   :8770  ──→ SQLite ~/.slack-mcp/slack.db     │
│                                                                       │
│  Each sync-MCP polls its upstream (Graph / ClickUp / Granola / Slack) │
│  on a schedule (delta/incremental sync) and writes to its local DB.   │
│  The MCP serves search/get/count tools over MCP-SSE.                  │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘
                       ▲ ▲ ▲ ▲ ▲
                       │ │ │ │ │
       ┌───────────────┘ │ │ │ └────────────────┐
   Microsoft         ClickUp Granola         Slack
   Graph             REST    REST            Web API
   (mail, OneNote)
```

---

## The six services

| Service | Port | Sync cadence | Auth | Backing API |
|---|---|---|---|---|
| `email-mcp`   | 8765 | full sync once, delta every 15 min | MSAL device-code (Azure app)             | Microsoft Graph `/me/messages` |
| `clickup-mcp` | 8767 | full sync once, delta every 10 min | Personal API Token (`pk_…`)              | ClickUp REST v2 |
| `granola-mcp` | 8768 | full sync once, delta every 30 min  | Enterprise API Key (`sk_…`)              | `https://public-api.granola.ai/v1` |
| `onenote-mcp` | 8769 | full + structure-sync, delta every 30 min, structure-resync every 4h | MSAL (same Azure app as email) | Microsoft Graph `/me/onenote` |
| `slack-mcp`   | 8770 | full sync once, delta every 15 min, channel re-enum every 30 min | User OAuth Token (`xoxp-…`) | Slack Web API |
| `chat-mcp`    | 8082 | n/a (web UI)                       | none (LAN+Tailscale boundary)            | local Ollama (`qwen3:32b`) |

All sync-MCPs share the same shape (`auth.py`, `database.py`, `graph.py`/`api.py`,
`sync.py`, `tools.py`, `server.py`, `main.py`), so editing one and you've edited
the pattern for all.

### Why these specific MCPs?

- The Anthropic-hosted `claude.ai` connectors (Slack, Google Drive, Microsoft 365)
  exist but require auth flows and don't give you a local cache or full-text search.
  These local MCPs let Claude (and any other MCP client) query our data offline
  with FTS5 across the entire history, even when upstream APIs are slow or
  rate-limited.
- The chat UI is a frontend for the same MCPs, using local Nemotron/qwen3 so we
  never send personal data over the wire to a third-party LLM.

---

## Hosts and paths

### <runtime-host> (current production)

```
~/projects/
├── email-mcp/                  # source code
├── clickup-mcp/
├── granola-mcp/
├── onenote-mcp/
├── slack-mcp/
├── chat-mcp/
├── README.md                   # this file
└── .git/                       # mono-repo

~/.email-mcp/                   # per-service data dirs (NOT in repo)
├── mail.db                     # SQLite cache + FTS5 (~4.5 GB)
├── token_cache.json            # MSAL token cache (chmod 600)
└── sync.log
~/.clickup-mcp/   → clickup.db, config.json (pk_… token), sync.log
~/.granola-mcp/   → granola.db, config.json (sk_… key),   sync.log
~/.onenote-mcp/   → onenote.db, token_cache.json,         sync.log
~/.slack-mcp/     → slack.db,   config.json (xoxp-…),     sync.log
~/.chat-mcp/      → config.json (model + MCP URLs + hidden_tools), chat.log

~/.config/systemd/user/         # systemd unit files
├── email-mcp.service           # auto-starts at login (linger enabled)
├── clickup-mcp.service
├── granola-mcp.service
├── onenote-mcp.service
├── slack-mcp.service
└── chat-mcp.service
```

### <source-host> (decommissioned but data preserved)

Source trees were rsynced from <source-host> to <runtime-host> on 2026-05-20.
<source-host> still has all 5 data dirs under `~/.{service}-mcp/` as a backup —
they're a point-in-time snapshot from migration day. The systemd services are
**disabled** there so they don't conflict with <runtime-host>'s syncing.

To roll back to <source-host> if <runtime-host> dies:
```
ssh <source-host>
systemctl --user enable --now email-mcp clickup-mcp granola-mcp onenote-mcp slack-mcp
```

(But you'd lose any data accumulated on <runtime-host> since 2026-05-20.)

---

## Operations cheatsheet

```bash
# Service control
systemctl --user status  email-mcp
systemctl --user restart slack-mcp
systemctl --user start   chat-mcp
systemctl --user stop    granola-mcp
systemctl --user list-units --type=service "*-mcp.service"

# Logs (last 50 lines per service)
for s in email clickup granola onenote slack chat; do
  echo "=== $s ==="; tail -50 ~/.${s}-mcp/*.log
done

# Quick health check (HTTP probe each)
for entry in "email:8765" "clickup:8767" "granola:8768" "onenote:8769" "slack:8770" "chat:8082"; do
  name=${entry%:*}; port=${entry#*:}
  printf "  %-8s :%s  HTTP %s\n" "$name" "$port" "$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:$port/healthz 2>/dev/null || curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:$port/)"
done

# Data freshness
bash /tmp/freshness_check.sh   # see this repo for the script

# Trigger an out-of-cycle delta (per service via its MCP tool)
# from any MCP client, call e.g. slack_force_sync (admin tool, hidden from LLM but exposed in API)

# Tail the chat UI's request log
journalctl --user -u chat-mcp -f
```

---

## Configuration files

### `~/.chat-mcp/config.json` (the chat UI's only config)

```json
{
  "llm": {                                    
    "endpoint": "http://127.0.0.1:11434",     // local Ollama
    "api_key": "ollama",                      // Ollama ignores
    "model": "qwen3:32b",                     // see "Model selection" below
    "max_tokens": 8000
  },
  "mcp_servers": {
    "email":   "http://127.0.0.1:8765/sse",
    "clickup": "http://127.0.0.1:8767/sse",
    "granola": "http://127.0.0.1:8768/sse",
    "onenote": "http://127.0.0.1:8769/sse",
    "slack":   "http://127.0.0.1:8770/sse"
  },
  "bind": { "host": "0.0.0.0", "port": 8082 },
  "hidden_tools": [
    "email_sync_status", "email_force_sync",
    "clickup_sync_status", "clickup_force_sync", "clickup_get_member",
    "granola_sync_status", "granola_force_sync",
    "onenote_sync_status", "onenote_force_sync",
    "slack_sync_status", "slack_force_sync"
  ]
}
```

The `hidden_tools` list is filtered out of the OpenAI tool schema passed to
the LLM (saves ~30% of prompt tokens per turn). The tools are still callable
directly via the MCP API for operational scripts.

### Per-service auth tokens

| Service | File | Type | How to regenerate |
|---|---|---|---|
| email   | `~/.email-mcp/token_cache.json`   | MSAL serialized cache | Delete, re-run `main.py` → device-code flow |
| clickup | `~/.clickup-mcp/config.json` (`{"token": "pk_..."}`)    | Personal API Token | ClickUp avatar → Settings → Apps → Generate |
| granola | `~/.granola-mcp/config.json` (`{"api_key": "sk_..."}`) | Enterprise API Key | Granola → Settings → Workspaces → API → Generate |
| onenote | `~/.onenote-mcp/token_cache.json` | MSAL serialized cache | Delete, re-run `main.py` → device-code flow (uses same Azure app as email) |
| slack   | `~/.slack-mcp/config.json` (`{"token": "xoxp-..."}`)  | User OAuth Token | api.slack.com/apps → app → OAuth & Permissions → Reinstall |

### Azure app (shared between email-mcp and onenote-mcp)

- App name: `<your-app-name>`
- Client ID: `<your-azure-app-client-id>`
- Tenant ID: `<your-azure-tenant-id>`
- Defaults baked into `email_mcp/auth.py` and `onenote_mcp/auth.py` — no env var needed
- Delegated permissions granted: `Mail.Read`, `Mail.ReadWrite`,
  `Notes.Create`, `Notes.Read`, `Notes.Read.All`, `Notes.ReadWrite`
- **Not granted:** `Notes.ReadWrite.All` (write to shared notebooks). The
  `onenote_mcp` write tools check `notebooks.is_shared` and refuse before
  the request would 403.

---

## Adding a new MCP source (template walkthrough)

Use `clickup-mcp/` as your template — it's the simplest one with token+REST.

1. `cp -r ~/projects/clickup-mcp ~/projects/<new-source>-mcp`
2. Rename inner package: `mv <new>-mcp/clickup_mcp <new>-mcp/<new>_mcp`
3. `find . -type f -exec sed -i 's/clickup/<new>/g; s/ClickUp/<New>/g' {} +`
4. Rewrite `<new>_mcp/api.py` for the new HTTP API (rate-limit aware, retry on 429/5xx).
5. Adjust schema in `<new>_mcp/database.py` (keep WAL + FTS5 contentless table pattern if you need full-text).
6. Adjust `<new>_mcp/sync.py` for full/delta semantics of the new API.
7. Update tool surface in `<new>_mcp/tools.py` — at minimum: `list_*`, `search_*`, `get_*`, `count_*`, `sync_status`, `force_sync`.
8. `<new>_mcp/server.py` — change port to a free one (8771+) and class name.
9. Update `<new>-mcp.service`: working dir, exec start, log path.
10. `./install.sh` → starts venv, copies systemd unit, enables (not starts).
11. `systemctl --user start <new>-mcp.service` → interactive first run to paste/save token.
12. Add to `~/.chat-mcp/config.json` under `mcp_servers`. Restart chat-mcp.
13. Verify chat-mcp picks up the new tools: `curl localhost:8082/api/mcp_status`.

### Per-MCP "must have" tool naming convention

The chat orchestrator prepends the server name + `_` to tool names. So an LLM
sees e.g. `slack_count_messages`, `email_search_emails`. The split is on the
**first** underscore, so server names must not contain underscores.

---

## Adding a new chat tool surface

The chat-mcp exposes one synthetic tool that doesn't live in any MCP:

- **`multi_search(query, sources?, per_source_limit?)`** — server-side parallel
  fan-out across all 5 sources. Implemented in `chat/mcp_pool.py` as
  `MCPPool._multi_search`. Routed via a special case at the top of `MCPPool.call`.

To add more synthetic tools, follow the same pattern: extend
`all_tools_as_openai_schema()` to register the schema and add a routing case
at the top of `call()`.

---

## Model selection / LLM tuning

### Why qwen3:32b?

- `nemotron-3-super:120b-a12b` is the highest-quality model on <runtime-host> but
  burns reasoning tokens before answering ⇒ every query is slow.
- `qwen3-coder:30b` is purpose-built for tool calling but **crashes the llama runner
  on this GB10 chip** (status 2, exit on load).
- `qwen3:32b` works, supports tool calling cleanly, ~5–8s warm latency per turn.

### Switching the model

Edit `~/.chat-mcp/config.json`:
```json
"model": "nemotron-3-super:120b-a12b"
```
then `systemctl --user restart chat-mcp`. List available models with
`curl http://127.0.0.1:11434/v1/models`.

### Keep-alive

The chat-mcp passes `keep_alive: "2h"` to Ollama on every request so the model
stays in VRAM between user messages. Without this, Ollama unloads after 5 min
of idle and the next query pays a 25-30s cold-load.

To preload after a service restart:
```bash
curl -s -X POST http://127.0.0.1:11434/api/chat \
  -d '{"model":"qwen3:32b","messages":[{"role":"user","content":"warmup"}],
       "keep_alive":"2h","stream":false}' >/dev/null
```

---

## Known issues, gotchas, and workarounds

These are real bugs we hit during the build — keep this section honest and
appended-to as you find more.

### 1. MSAL rejects `offline_access` in scopes list

MSAL hard-rejects `offline_access` as a "reserved scope" even though Graph
docs say to request it. **Drop it from the SCOPES list.** MSAL adds it
internally. Both `email_mcp/auth.py` and `onenote_mcp/auth.py` already do this.

### 2. Starlette ≥ 1.0 removed `Request._send`

The MCP SDK's SSE server examples reach into `request._send`. That field is
gone in starlette 1.x. **All our MCP servers use a raw ASGI app instead**
(`_EmailMCPApp.__call__(scope, receive, send)`) — copy that pattern if you
add new MCPs.

### 3. Microsoft Graph delta token case sensitivity

Graph's `@odata.deltaLink` uses `$deltatoken` (lowercase) in the URL query.
Earlier code looked for `$deltaToken` (capital T) and consequently never
persisted any token, causing every delta sync to re-fetch the same 9,688
messages. Fixed in `email_mcp/graph.py` — accepts either case.

### 4. OneNote 5,000-OneNote-items SharePoint threshold

If your OneDrive has > 5,000 OneNote items (notebooks + sections + section
groups), `/me/onenote/notebooks/{id}/sections` returns HTTP 403 with error
code 10008. Our workaround: use the flat `/me/onenote/sections?$expand=parentNotebook`
endpoint and derive notebook membership from each section's parent. See
`onenote_mcp/sync.py:_sync_notebook_structure_flat`.

Some individual sections also return HTTP 422 error 20258 ("Sync of this
section is not supported") — these are silently skipped. They're typically
old Evernote-import sections.

### 5. OneNote delta sync uses `/me/onenote/pages` which 400s on big libraries

Same root cause as #4. Our delta sync now iterates per-section via
`iter_pages_in_section(section_id)` and walks newest-first, stopping when it
hits a page older than `last_delta_sync_cutoff`. See `onenote_mcp/sync.py:run_delta_sync`.

### 6. APScheduler + lambda + `asyncio.create_task` = no running event loop

Original code wrapped async sync functions in `lambda: asyncio.create_task(...)`
inside `AsyncIOScheduler.add_job`. The lambda gets run in a thread executor
that has no event loop and `create_task` raises. **Pass the coroutine function
directly:** `add_job(sync.run_delta_sync, args=[get_token], ...)`. All 6
main.py files were fixed in this session.

### 7. Slack delta sync at 5min interval piles up

Slack's `conversations.history` for a busy workspace takes > 5 minutes per
full pass. APScheduler logged 206 "max instances reached" warnings before
we bumped the interval to 15 min. Don't go below 15 min for delta unless
your workspace is small.

### 8. qwen3-coder:30b crashes Ollama on GB10

`qwen3-coder:30b` (Q4_K_M) terminates the llama runner with exit status 2
on first load. Use `qwen3:32b` (not Coder) instead — same speed class,
clean tool calling, no crashes. Suspected ARM-specific MoE-runtime
incompatibility, not a model issue per se.

### 9. Ollama default `keep_alive` unloads model after 5 min

If you don't pass `keep_alive` in the request, Ollama unloads the model
after 5 min of idle, causing the next request to pay a cold-load delay.
chat-mcp passes `keep_alive: "2h"` on every request.

Conversely, the previous Nemotron load was pinned with `keep_alive` set
to year 2318 (effectively infinity). Trying to load a second model failed
with OOM. To unload an over-pinned model:
```bash
curl -X POST http://127.0.0.1:11434/api/generate \
     -d '{"model":"<name>","keep_alive":0}'
```

### 10. Tool surface bloat hurts model performance

55 tool schemas (~6-8K tokens of prompt overhead per turn) was eating context
and slowing every response. We hide ~11 admin/diagnostic tools from the LLM
via `hidden_tools` in config. Anything you'd never expect the LLM to call
(force_sync, sync_status, get_member, etc.) belongs in that list.

### 11. Counting via list pagination → infinite loop

Tools that return paginated lists (`get_messages_to_user`, `get_emails_by_folder`)
will be brute-force-counted by the LLM if asked "how many X". The LLM hits
the 10-iteration cap. Add a dedicated `count_*` tool that does server-side
aggregation. We did this for slack and email; clickup/granola/onenote could
use the same treatment if you start hitting it.

### 12. Tailscale Serve needs per-tailnet enablement

`tailscale serve` returns "Serve is not enabled on your tailnet" until you
visit the URL it prints (an admin in your tailnet has to enable it once).
We bypassed Tailscale Serve entirely on <runtime-host> — chat-mcp just binds
to `0.0.0.0:8082` and Tailscale's normal magicDNS makes it reachable. No
HTTPS — but since there's no public endpoint, it's tailnet-only access.

### 13. Granola list endpoint doesn't return calendar event details

`GET /v1/notes` returns metadata only — no attendees, no organiser_email, no
calendar_event_id. To get those you'd need to hit `GET /v1/notes/{id}` per note,
which is rate-limited. The DB schema has the fields but they're all NULL until
you fetch each note individually (which `get_note` does lazily).

### 14. chat-mcp starts before sync-MCPs are ready

On a cold systemd start, chat-mcp starts in parallel with the 5 sync-MCPs,
but the sync-MCPs run their initial delta-sync catchup before binding their
HTTP port. Chat-mcp's connection pool will log "MCP[<name>] connect failed"
warnings for ones still booting. There's a reconnect-on-call path that
handles this gracefully — first user query may reconnect, or you can
`systemctl --user restart chat-mcp` to get clean log lines. Could add
`After=email-mcp.service slack-mcp.service` to the unit if you want strict
ordering, but it's not strictly needed.

---

## Recovery / disaster scenarios

### "I think a sync is broken"

```bash
tail -30 ~/.<service>-mcp/sync.log    # look for errors today
sqlite3 ~/.<service>-mcp/*.db 'SELECT * FROM sync_state'  # last sync timestamps
bash /tmp/freshness_check.sh           # comprehensive snapshot (script in this repo)
```

### "Token expired / revoked"

You'll see HTTP 401 in the sync log. For each service the fix:

- **email / onenote:** `rm ~/.<service>-mcp/token_cache.json`, then run `~/projects/<service>-mcp/venv/bin/python ~/projects/<service>-mcp/main.py` interactively. Device-code flow will re-prompt.
- **clickup / granola:** edit `~/.<service>-mcp/config.json` and paste a fresh token, then `systemctl --user restart <service>-mcp`.
- **slack:** Reinstall the app at api.slack.com/apps to get a new `xoxp-…`, then edit config and restart.

### "I want to nuke a service and start fresh"

```bash
systemctl --user stop <svc>-mcp
rm -rf ~/.<svc>-mcp/<db file>          # blow away just the cache
rm ~/.<svc>-mcp/<sync state if separate>
systemctl --user start <svc>-mcp        # full sync from scratch (could take hours for email)
```

### "Migrate to another host"

This is what we did 2026-05-20 (<source-host> → <runtime-host>). High-level:

1. Stop all sync services on source host.
2. `rsync -avz --exclude='venv/' --exclude='__pycache__/' ~/projects/ <newhost>:~/projects/`
3. `rsync -az ~/.{email,clickup,granola,onenote,slack,chat}-mcp ~ <newhost>:~/`
4. On new host: `cd ~/projects/<svc> && ./install.sh` for each project.
5. `systemctl --user daemon-reload && systemctl --user enable --now {all}-mcp.service`
6. Verify each via the health-check loop above.
7. Disable services on source host (but keep data dirs as backup).

Tokens are portable — MSAL caches are JSON without machine binding, and the
opaque tokens (`pk_…`, `xoxp-…`, `sk_…`) are server-side strings.

---

## Tool inventory (LLM-visible, as of 2026-05-20)

Each tool below is prefixed with `<server>_` when exposed to the LLM (so e.g.
`slack_count_messages`). Admin tools (`sync_status`, `force_sync`,
`get_member`) are hidden via `hidden_tools` in config but remain callable
directly via the MCP API.

### email (8 LLM-visible + 1 admin hidden)
`list_folders`, `search_emails`, `get_emails_by_folder`, `get_email`,
`get_thread`, `create_draft`, `count_messages`. Hidden: `sync_status`, `force_sync`.

### slack (12 LLM-visible + 2 admin hidden)
`list_workspaces`, `list_channels`, `list_users`, `get_channel_messages`,
`get_thread`, `get_message`, `search_messages`, `get_messages_from_user`,
`get_messages_to_user`, `get_user`, `count_messages`. Hidden: `sync_status`,
`force_sync`.

### clickup (13 LLM-visible + 3 admin/hidden)
`list_workspaces`, `list_spaces`, `list_folders`, `list_lists`, `search_tasks`,
`get_tasks`, `get_task`, `get_subtasks`, `get_task_comments`, `create_task`,
`update_task`, `add_comment`. Hidden: `get_member`, `sync_status`, `force_sync`.

### granola (7 LLM-visible + 2 admin hidden)
`list_folders`, `list_notes`, `search_notes`, `get_note`, `get_transcript`,
`get_notes_by_folder`, `get_notes_by_attendee`. Hidden: `sync_status`, `force_sync`.

### onenote (9 LLM-visible + 2 admin hidden)
`list_notebooks`, `list_sections`, `list_pages`, `get_page`, `search_pages`,
`get_recent_pages`, `create_page`, `append_to_page`, `replace_page_content`.
Hidden: `sync_status`, `force_sync`.

### chat-mcp synthetic (1)
`multi_search(query, sources?, per_source_limit?)` — server-side parallel
fan-out across all 5 sources.

**Total visible to LLM: 47 tools.**

---

## Build history (brief timeline)

- **2026-05-19 morning** — Built `email-mcp` on <source-host>. First MSAL device-code flow,
  full sync of 196k messages.
- **2026-05-19 midday** — Built `clickup-mcp`, `granola-mcp` via subagents (same pattern).
- **2026-05-19 afternoon** — Built `onenote-mcp`. Discovered the 5,000-item Graph limit;
  reworked to flat-section enumeration with `$expand=parentNotebook`. Granted Notes scopes
  to the existing Azure app.
- **2026-05-19 evening** — Built `slack-mcp` after enabling user OAuth on a fresh Slack app.
- **2026-05-20 morning** — Built `chat-mcp` for the chat UI. Initially pointed at the operator's organisation's
  remote OpenAI-compatible LLM gateway with Nemotron. Hit various Tailscale Serve enablement issues.
- **2026-05-20 midday** — Migrated everything from <source-host> to <runtime-host> so the LLM could
  run locally. Dropped Tailscale Serve in favor of binding to `0.0.0.0:8082`. Switched
  model to local Ollama qwen3:32b (after qwen3-coder crashed on GB10).
- **2026-05-20 afternoon** — Optimization pass: hidden_tools filter, `count_messages` for
  slack + email, `multi_search` synthetic tool, system-prompt tool-selection guidance,
  `keep_alive: 2h` so the model doesn't unload between requests.

---

## Future improvements (deferred)

- Add `count_*` aggregation tools to clickup, granola, onenote (same pattern as slack/email).
- Add a model picker in the UI so you can switch between qwen3:32b (fast) and
  nemotron-3-super:120b (high quality) per-conversation.
- HTTPS via Caddy + `tailscale cert` if you ever want browser cert padlock /
  service-worker installability.
- Auto-restart Ollama if model load fails (currently you'd notice via 500s in chat).
- Periodic backup of the data dirs to <source-host> (or to S3, or to wherever you trust).
- Inactive notebooks/sections in OneNote could be soft-archived from the LLM's view to
  cut down search noise.
- Add a real auth layer (Tailscale-User-Login header check) for if/when this ever
  leaves the LAN+tailnet boundary.

---

## Glossary

- **MCP** — Model Context Protocol. Anthropic's protocol for letting LLMs call out
  to external tools/data sources.
- **SSE** — Server-Sent Events. The HTTP transport MCP uses here (alternative to stdio).
- **FTS5** — SQLite's full-text-search extension. We use it for fast search across
  text fields in each cache.
- **Contentless FTS5 table** — FTS5 table not auto-mirrored from a single base table —
  used in slack-mcp because the search columns span `messages` + `users` + `conversations`.
- **Delta sync** — Incremental sync that only pulls changes since the last run, via
  delta tokens (Graph) or `updated_after` filters (ClickUp, Granola) or
  `oldest`/`latest` timestamps (Slack).
- **Full sync** — Initial enumeration that pulls everything. Resumable across restarts
  via `fully_synced` flags per folder/list/section/channel.
- **Linger** — systemd feature (`loginctl enable-linger`) that lets user services
  keep running across logouts and through reboots. Enabled on <runtime-host>.
- **GB10** — Nvidia Grace Blackwell GB10 chip. ARM CPU + unified-memory GPU. Some
  CUDA-targeted models crash on it; qwen3:32b works.
