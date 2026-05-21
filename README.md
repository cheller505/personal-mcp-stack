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

## Stack at a glance

| Component       | Default                                     | Where to configure |
|---|---|---|
| LLM             | **`qwen3:32b`** (via local [Ollama](https://ollama.com)) | `~/.chat-mcp/config.json` → `llm.model` |
| Chat UI port    | `:8082`                                     | `~/.chat-mcp/config.json` → `bind` |
| email-mcp port  | `:8765`                                     | `EMAIL_MCP_PORT` env var |
| clickup-mcp port| `:8767`                                     | `CLICKUP_MCP_PORT` env var |
| granola-mcp port| `:8768`                                     | `GRANOLA_MCP_PORT` env var |
| onenote-mcp port| `:8769`                                     | `ONENOTE_MCP_PORT` env var |
| slack-mcp port  | `:8770`                                     | `SLACK_MCP_PORT` env var |
| Sync cadence    | 10–30 min (per source)                      | per-MCP `main.py` APScheduler interval |
| Storage         | per-source SQLite under `~/.<svc>-mcp/`     | not configurable (relative to `$HOME`) |

The default model `qwen3:32b` was chosen for ~5–8 s warm tool-call latency
with strong tool-calling accuracy; see `OPERATIONS.md` gotcha #8 for why
`qwen3-coder:30b` was rejected and `nemotron-3-super:120b-a12b` is supported
but slow. You can swap to anything else OpenAI-compatible (local or remote)
without code changes — see the [Local LLM (Ollama)](#local-llm-ollama)
section below.

## Credentials at a glance

You need credentials only for the sources you want to sync. Each link below
goes to the page where you generate the token. The full step-by-step is in
the [Credentials you'll need](#credentials-youll-need) section below; this
table is the index.

| Source        | What you generate            | Where                                                       |
|---|---|---|
| email-mcp     | Azure app + delegated scopes | https://portal.azure.com → App registrations                |
| onenote-mcp   | (reuses email-mcp's Azure app) | same as above, just add `Notes.*` permissions             |
| clickup-mcp   | Personal API Token (`pk_…`)  | https://app.clickup.com → avatar → Settings → Apps          |
| granola-mcp   | Enterprise API Key (`sk_…`)  | Granola → Settings → Workspaces → API (Enterprise plan)     |
| slack-mcp     | User OAuth Token (`xoxp-…`)  | https://api.slack.com/apps → new app → User Token Scopes    |
| chat-mcp LLM  | nothing if local Ollama; API key if remote endpoint | https://ollama.com (local) or your provider |

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

> ⚠️ **Security warning — the chat UI has no authentication.** The chat-mcp
> web client (default port `:8082`) ships with a permissive `allow_all`
> auth dependency, on the assumption that you'll only reach it over a
> trusted LAN or a private overlay (e.g. Tailscale). **Do not expose this
> port to the public internet.** Before starting the stack, confirm that
> `:8082` (and the MCP service ports `:8765–:8770` and Ollama `:11434`)
> are firewalled off from WAN — anyone who can reach `:8082` can chat with
> your data and trigger tool calls against your synced email/Slack/etc.

> Before either path: skim the [Credentials at a glance](#credentials-at-a-glance) table above to know what you'll be asked for. Then pick one path:

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

### Option C — Docker Compose install

If you'd rather not deal with venvs and systemd, every service has a
`Dockerfile` and the root has a `docker-compose.yml` that brings the whole
stack up.

```bash
cp .env.example .env
$EDITOR .env   # set EMAIL_MCP_CLIENT_ID, EMAIL_MCP_TENANT_ID, etc.
docker compose build
```

First-time auth (interactive, one per source that needs a token):

```bash
docker compose run --rm email-mcp     # device-code flow
docker compose run --rm onenote-mcp   # device-code flow
docker compose run --rm clickup-mcp   # paste token at prompt
docker compose run --rm granola-mcp   # paste key at prompt
docker compose run --rm slack-mcp     # paste token at prompt
```

Ctrl-C each one after auth completes — the token is saved into the named
volume. Then:

```bash
# Configure chat-mcp to use docker network service names (not localhost)
docker run --rm -v personal-mcp-stack_chat-data:/data alpine   sh -c "mkdir -p /data/.chat-mcp && cat > /data/.chat-mcp/config.json"   < chat-mcp/docker-config.example.json

docker compose up -d
# open http://localhost:8082/
```

Notes:
- The included `ollama` service uses the NVIDIA Container Toolkit; if you
  don't have a GPU or want to point at an existing Ollama, remove the
  `ollama` service block and change `llm.endpoint` in the chat config
  to your endpoint.
- All ports are configurable in `.env` (e.g. set `EMAIL_MCP_HOST_PORT=18765`
  to run the docker stack alongside an existing systemd install).
- Data persists in named docker volumes (`docker volume ls | grep
  personal-mcp-stack`). To migrate from an existing systemd install, copy
  `~/.<svc>-mcp/` contents into the matching volume.

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

## Credentials you'll need

You only need credentials for the sources you actually want to sync. Each link
below opens the page where you'll generate the token / API key / register the
app. **`SETUP.md` has the full step-by-step**, including scopes and gotchas;
this is just a one-stop summary of *what* you need *from where*.

### Microsoft 365 mail + OneNote (shared Azure app)

You register **one** Azure app and use it for both `email-mcp` and
`onenote-mcp`.

1. **https://portal.azure.com** → sign in with your work account.
2. **App registrations** → **New registration**.
   - Name: anything (e.g. `personal-mcp`)
   - Supported account types: usually "this organisational directory only"
   - Leave redirect URI blank.
3. After creation, copy the **Application (client) ID** and **Directory
   (tenant) ID**.
4. **API permissions** → **Add a permission** → **Microsoft Graph** →
   **Delegated permissions**, then add:
   - For email: `Mail.Read`, `Mail.ReadWrite`
   - For OneNote: `Notes.Read`, `Notes.Read.All`, `Notes.ReadWrite`,
     `Notes.Create`
   - **Do NOT add `offline_access`** — MSAL adds it implicitly and rejects
     explicit requests for it.
5. Click **Grant admin consent**. Your tenant may require an admin to
   approve.
6. **Authentication** → **Add a platform** → **Mobile and desktop
   applications** → tick `https://login.microsoftonline.com/common/oauth2/nativeclient`.
7. Set environment variables:
   ```bash
   export EMAIL_MCP_CLIENT_ID=<application-client-id>
   export EMAIL_MCP_TENANT_ID=<directory-tenant-id>
   export ONENOTE_MCP_CLIENT_ID=<same-application-client-id>
   export ONENOTE_MCP_TENANT_ID=<same-directory-tenant-id>
   ```
   (Or edit the systemd unit files at
   `~/.config/systemd/user/{email,onenote}-mcp.service`.)
8. Run each service interactively once to complete the device-code flow.
   A short code prints; visit `https://microsoft.com/devicelogin` on any
   browser, paste the code, sign in.

### ClickUp

1. Open **https://app.clickup.com** → click your avatar (bottom-left)
2. **Settings** → **Apps**
3. Under **API Token**, click **Generate** (token starts with `pk_…`)
4. Run `clickup-mcp/main.py` interactively; it prompts and saves to
   `~/.clickup-mcp/config.json`.

### Granola (Enterprise plan required)

1. Open Granola → **Settings** → **Workspaces** → **API** tab
2. Click **Generate API Key** (starts with `sk_…`)
3. Run `granola-mcp/main.py` interactively; saved to
   `~/.granola-mcp/config.json`.

### Slack (user-token, not bot-token)

1. **https://api.slack.com/apps** → **Create New App** → **From scratch** →
   pick your workspace
2. **OAuth & Permissions** → scroll to **User Token Scopes** (NOT the Bot
   Token Scopes section above it — they're separate boxes on the same page)
3. Add user scopes:
   ```
   channels:read    channels:history
   groups:read      groups:history
   im:read          im:history
   mpim:read        mpim:history
   users:read       users:read.email
   files:read       team:read        reactions:read
   ```
4. Scroll back up → **Install to Workspace** → approve. Your workspace admin
   may need to approve this.
5. After install, the page shows a **User OAuth Token** starting with `xoxp-…`.
   Copy it.
6. Run `slack-mcp/main.py` interactively; saved to
   `~/.slack-mcp/config.json`.

### Local LLM (Ollama)

Not credentials per se, but the chat UI needs an LLM endpoint:

1. Install [Ollama](https://ollama.com): `curl -fsSL https://ollama.com/install.sh | sh`
2. Pull a model with strong tool calling and a manageable size:
   - `ollama pull qwen3:32b` (~17 GB, recommended)
   - or `ollama pull qwen3-coder:30b` (similar size; may not work on all GPU
     architectures — see OPERATIONS.md gotcha #8)
   - or any other OpenAI-compatible model you prefer
3. Configure in `~/.chat-mcp/config.json` (see SETUP.md section 5b).

Alternative: any OpenAI-compatible remote endpoint (vLLM, LiteLLM,
OpenRouter, OpenAI itself) works just as well — set `llm.endpoint` and
`llm.api_key` accordingly. Data sent during chat will leave your network.

### Security notes for tokens

- All tokens go into files at `~/.<service>-mcp/{config.json|token_cache.json}`,
  always chmod 600.
- The repo's `.gitignore` excludes these paths defensively.
- Don't commit `~/.<service>-mcp/` content. Nothing under `~/.<service>-mcp/`
  is in the repo tree — it lives in your home directory.
- If a token leaks, rotate it at the source (regenerate in the upstream's
  settings) and update the local file. The MCPs will pick up the new value
  on next restart.

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
