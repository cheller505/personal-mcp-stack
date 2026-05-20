# Building a personal knowledge stack with MCP

> A long-form essay on why I built a local mirror of my email, Slack, task
> tracker, meeting notes, and notebook pages — and what I learned doing it in
> 36 hours.

## The problem

I work in research computing. My day's information lives in five systems:

- **Email** (Microsoft 365). 196,000 messages going back a decade.
- **Slack** (NCSA workspace). 51,000 messages across 1,500 conversations.
- **A task tracker** (ClickUp). 1,700 tasks, most stale.
- **AI meeting notes** (Granola). 242 transcribed/summarized meetings.
- **A notebook** (OneNote). Years of accumulated working notes.

Each system has a search box. None of them search the others. When I want to
answer "what did my manager and I discuss about that infrastructure decision" or
"what should I work on next week given everything that's been flying at me",
I can't.

I also can't easily *share* this context with an LLM. The Anthropic-hosted
connectors for these services exist, but they:

1. Send query traffic to a third party (fine for general questions, less great
   for sensitive infrastructure planning data).
2. Don't provide a *local cache* — every query is an API round-trip, subject
   to rate limits, network availability, and whoever owns those endpoints.
3. Are read-only point-in-time lookups, not searchable archives.

What I wanted: a local, fast, full-history mirror of everything, with an LLM
sitting on top that can ask questions across all of it.

## The shape of the solution

Each source becomes a **MCP server** — one process per source — that:

1. **Mirrors** the upstream API into a local SQLite database with an FTS5
   full-text index, doing a one-time full sync followed by periodic
   delta/incremental syncs.
2. **Exposes** read-only tools (search, get, list, count, and a few simple
   write tools) over [MCP-SSE](https://modelcontextprotocol.io/) so any MCP
   client can use them.

On top of that, a thin **chat web UI** acts as a MCP client itself:

- Connects to all five sync MCPs at startup, auto-discovers every tool.
- Adds two synthetic meta-tools: `multi_search` (fan-out keyword across all
  sources) and `priority_digest` (cross-source agenda/triage).
- Talks to a local LLM (Ollama with qwen3:32b on a unified-memory GPU) using
  the OpenAI-compatible chat-completions API with tool calling.
- Streams responses to a vanilla HTML chat page over SSE.

Everything runs on one machine. Nothing on the chat path leaves the local
network.

```
┌─ <runtime-host> (local GPU host) ──────────────────────────────────────────┐
│                                                                     │
│  Chat web UI ──→  Local LLM (Ollama, qwen3:32b)                     │
│       │                                                             │
│       ├──→  email-mcp    →  SQLite + FTS5                           │
│       ├──→  slack-mcp    →  SQLite + FTS5                           │
│       ├──→  clickup-mcp  →  SQLite + FTS5                           │
│       ├──→  granola-mcp  →  SQLite + FTS5                           │
│       └──→  onenote-mcp  →  SQLite + FTS5                           │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

## Why MCP and not just direct DB access

Two reasons:

1. **The chat UI isn't the only consumer.** Claude Desktop, Claude Code, and
   anything else speaking MCP can connect to the same set of sync MCPs and
   get the same tools. The UI is one client among many. Direct SQL access
   would be tied to one app.

2. **The abstraction is good.** Each MCP encapsulates schema, FTS quirks,
   rate limiting, auth refresh, and result formatting behind a clean tool
   surface. The LLM doesn't need to know that "search emails" maps to a join
   between `messages` and `messages_fts` with a particular `MATCH` operator
   and a snippet function — it just calls `email_search_emails(query="...",
   limit=20)` and gets human-readable text back.

The cost is some redundancy — every sync MCP has its own database, even
though they could share one. That's fine for an MVP and arguably better:
schema independence per source means I can rip out and replace a single
source without touching the others.

## The shape of each sync MCP

All five sync MCPs share the same skeleton:

```
<service>_mcp/
├── auth.py        # token handling (OAuth device flow, paste-token, etc.)
├── database.py    # SQLite schema, indexes, FTS5 triggers
├── api.py         # async HTTP client with rate limiting
├── sync.py        # full + delta sync logic
├── tools.py       # MCP tool definitions and handlers
└── server.py      # raw ASGI MCP-SSE server
main.py            # entry point: auth → init_db → sync → schedule → serve
install.sh         # venv + systemd unit
<service>-mcp.service  # systemd user unit
```

If you've written one, you've written all of them — the differences are
upstream-API specifics (REST shape, rate limits, pagination, delta semantics).
I shipped five of these in 36 hours largely because of this consistency:
spawn a subagent with the template plus a brief on the new API, get a
working implementation back, iterate.

### Sync strategy

For each source:

1. **Full sync** on first run. Walks the entire upstream history, paginated.
   **Resumable**: per-entity flags (`fully_synced`) so a restart doesn't
   start over.
2. **Delta/incremental sync** every 10–30 min. Uses whatever the upstream
   provides:
   - Microsoft Graph: delta tokens (per folder for email, per section for
     OneNote pages)
   - ClickUp: `date_updated_gt` filter per list
   - Granola: `updated_after` parameter
   - Slack: `oldest` timestamp per channel; also `conversations.list` on a
     longer cadence to pick up new channels

3. **Pending-retry loop** for cases where data isn't ready yet (Granola
   doesn't surface notes until the AI summary completes).

## The chat UI

```
fastapi (chat-mcp)
├── auth.py              # stubbed permissive (LAN/tailnet boundary)
├── config.py            # loads ~/.chat-mcp/config.json
├── mcp_pool.py          # persistent MCP clients, tool auto-discovery
├── llm.py               # OpenAI-compatible async client (httpx, SSE)
├── orchestrator.py      # tool-calling loop, streams to UI
├── server.py            # FastAPI routes + SSE
└── ui.html              # single-file vanilla HTML chat page
```

At startup the chat UI opens a persistent SSE connection to each sync MCP,
calls `list_tools()`, and caches the combined schema. The OpenAI tool schema
sent to the LLM is the union of all per-source tools (each prefixed with
the source name, e.g. `slack_search_messages`), minus a small `hidden_tools`
list (admin/diagnostic tools that the LLM doesn't need to see), plus the
two synthetic tools.

When the LLM emits tool calls, the orchestrator dispatches them via the
pool in parallel (`asyncio.gather`), feeds the results back, and loops.
Bounded at 10 iterations to prevent runaway tool-call cycles.

## Six lessons learned

### 1. MCP SDK examples are subtly broken with current Starlette

The MCP Python SDK's SSE-server example reaches into a private
`request._send` attribute on the Starlette `Request` object. Starlette 1.0
removed that. Every sync MCP here uses a **raw ASGI app** instead of
Starlette `Route` objects — a small class with `async def __call__(self,
scope, receive, send)` that dispatches `/sse`, `/messages`, and lifespan
events. About 60 lines, works fine, future-proof.

### 2. MSAL rejects `offline_access` in the scopes list

Microsoft's docs say to request `offline_access` for refresh tokens. MSAL
hard-rejects it as a "reserved scope". MSAL adds it implicitly — you have to
omit it from your scope list. We hit this on first run of email-mcp.

### 3. Microsoft Graph delta token case sensitivity

Graph's `@odata.deltaLink` returns the next delta token as
`$deltatoken` (lowercase). Our first parser looked for `$deltaToken`
(capital T) and consequently never stored any token. Every "incremental"
sync re-fetched the same 9,688 messages indefinitely. The fix is one-line —
accept either case — but the symptom (sync looks like it's working, just
inefficient) is the kind of bug you only catch by checking that the delta
state actually advances between runs.

### 4. OneNote's 5,000-item ceiling

If your OneDrive has more than 5,000 OneNote items (notebooks + sections +
section groups), `/me/onenote/notebooks/{id}/sections` returns 403 with
error 10008. This is a SharePoint list-view threshold leaking into the
OneNote API. Top-down enumeration is dead.

Workaround: hit the flat `/me/onenote/sections?$expand=parentNotebook`
endpoint, which doesn't trigger the per-notebook query and works fine. Then
for each section that's reachable, paginate its pages. Some legacy sections
(typically Evernote imports from years ago) still return 422 error 20258
("Sync of this section is not supported") — those are gracefully skipped.

Net effect on a real account: 7 notebooks visible, 62 sections reachable
out of likely many more, 3,000 pages cached. The unreachable content stays
unreachable until you prune your OneDrive.

### 5. APScheduler + `asyncio.create_task` in a lambda is wrong

Original code wrapped async sync functions like this:

```python
scheduler.add_job(
    lambda: asyncio.create_task(sync.run_delta_sync(get_token)),
    trigger="interval", minutes=15,
)
```

APScheduler's `AsyncIOScheduler` runs the lambda in a thread executor
that has no event loop. `asyncio.create_task` raises
`RuntimeError: no running event loop`. Symptom: scheduler fires on schedule,
no sync happens, errors show up in journalctl but the service is "running."

Fix: pass the coroutine function directly:

```python
scheduler.add_job(
    sync.run_delta_sync,
    args=[get_token],
    trigger="interval", minutes=15,
)
```

APScheduler awaits async callables on the event loop natively. Hit this in
every single sync MCP because the template propagated.

### 6. Tool surface bloat hurts the LLM

55 tools across 5 sources = ~6–8K tokens of overhead in the system prompt
*every turn*. That's both expensive and distracting — small/medium models
get noticeably worse at tool selection when the schema is bloated.

Two interventions helped:
- A `hidden_tools` config that filters admin/diagnostic tools (force_sync,
  sync_status, etc.) from the LLM-facing schema. They stay callable directly
  via the MCP API for ops scripts.
- Synthetic meta-tools (`multi_search`, `priority_digest`) that the LLM
  reaches for first when the question is broad. One call instead of five
  sequential per-source searches.

### 7 (bonus). "How many X" devolves into pagination loops

Tools that return paginated lists (`get_messages_to_user`,
`get_emails_by_folder`) get brute-force-counted by the LLM when you ask
"how many X". It pages through, hits the iteration cap, gives up, and tells
you it can't.

Fix: add `count_*` tools per source that do server-side aggregation,
optionally with `group_by`. Returns a single number (or a histogram if
grouped) in one call. We added these for slack and email; the others would
benefit too.

## Model selection on a unified-memory GPU

The target host is a Grace-Blackwell ARM system with unified memory. Three
candidates, three behaviors:

- `nemotron-3-super:120b-a12b` — works, high quality, but burns reasoning
  tokens on every turn. ~30s warm latency per query. Best for deep
  multi-source synthesis questions.
- `qwen3-coder:30b` — purpose-built for tool calling. Crashes the llama
  runner with exit status 2 on first load. Suspected ARM/MoE-runtime
  incompatibility.
- `qwen3:32b` — works, supports tool calling cleanly, ~5–8s warm latency.
  Sweet spot for daily use.

Lesson: don't pick a model on benchmarks alone — test it on your actual
hardware. The 30B vs 120B parameter count was misleading; the *architecture*
(MoE quirks, runner support) mattered more.

One Ollama-specific gotcha: by default the model unloads after 5 min idle.
Every "first" question of a session then pays a 25–30s cold-load tax.
Setting `keep_alive: "2h"` on each request keeps the model resident, paid
for by ~20GB of VRAM you're going to use anyway.

## What I'd do differently

- **Shared database.** Five separate SQLite files complicate cross-source
  joins. The `priority_digest` tool opens each in turn. A single DB with
  per-source tables would simplify aggregate analytics (and might be a
  perfectly fine choice — SQLite scales further than people think).
- **Semantic search.** FTS5 is fast and good for keyword queries. For
  fuzzy questions ("emails about the GPU procurement decision"), an
  embedding layer would help. The MCP shape doesn't care — adding a
  `semantic_search` tool is straightforward.
- **A unified "Sync" abstraction.** Each sync MCP has its own slightly
  different `sync.py` doing the same things (full sync, delta sync,
  resumability, error handling, logging). A small framework module that
  the per-source code subclasses would cut total LOC and make new sources
  faster to add.
- **Stronger auth from day one.** The chat UI's auth is stubbed because
  the deployment is on a trusted network. That's fine for now but a real
  password / passkey gate is one well-understood layer that I keep
  putting off.

## What it cost

- ~36 hours of focused work over two days (with one major host migration
  in the middle — from a tailscale-only laptop to a local GPU host so the
  LLM could run on the same machine as the MCPs).
- Trivial dollar cost: one Slack app registration, one Azure app
  registration, both free. The GPU host was already provisioned.
- One reasonable amount of "you can't roll back the personal data" risk,
  mitigated by keeping the source-host data dirs as a point-in-time backup
  after migrating.

## What it bought

Searching my entire work history across five systems in milliseconds.
Asking an LLM "what should I prioritize next week" and getting a synthesized
cross-source answer rather than five separate apps' answers. The ability to
hand the same MCPs to Claude Code so my coding assistant has the same
context I do. The data is mine, on my hardware, queryable in any future
client that speaks MCP — which, increasingly, will be most of them.

Worth it.

## Recipe summary

For someone wanting to replicate:

1. Pick which sources matter. Each one is independent — start with the one
   where lack of search hurts most.
2. Use the per-source MCP as a template. The skeleton is identical;
   substitute the API client and adapt the schema.
3. Use the chat UI verbatim. The hard work is the orchestrator + MCP pool;
   the UI is throwaway HTML.
4. Run a small-to-medium LLM locally if hardware allows; otherwise route
   to a remote OpenAI-compatible endpoint of your choice.
5. Document the gotchas as you find them. The list above is incomplete and
   will grow as APIs change.

The code is at <add-repo-link-when-published>. License: MIT. PRs welcome,
especially for new source MCPs.
