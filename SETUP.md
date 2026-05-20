# Setup guide

End-to-end walkthrough to stand up the personal knowledge stack on a fresh
Linux host. Plan for 60–90 minutes if all your upstream creds are ready, longer
if you need to register an Azure app or wait for admin approvals.

---

## Prerequisites

- Linux (tested on Ubuntu 24.04, both x86 and ARM/GB10). Probably works on
  macOS too with minor systemd adjustments — use `launchd` instead.
- Python 3.11 or newer
- `git`, `rsync`, `curl`, `sqlite3`
- Enough disk for the email cache (allow ~5GB for tens of thousands of messages
  with full bodies; slack/clickup/granola/onenote are an order of magnitude
  smaller each)
- An LLM endpoint:
  - **Recommended:** local [Ollama](https://ollama.com) with `qwen3:32b` (works
    on consumer GPUs / unified-memory machines) or any other OpenAI-compatible
    serving stack you already run
  - **Alternate:** any OpenAI-compatible remote endpoint (your enterprise
    LiteLLM, vLLM, OpenRouter, etc.)
- Per-source credentials — see the per-source sections below

## 1. Clone and bootstrap

```bash
cd ~
git clone <this-repo>.git projects
cd projects
./bootstrap.sh
```

`bootstrap.sh` walks you through picking which sources you want and runs each
`install.sh`. You can also install services individually — `bootstrap.sh` is
just a wrapper.

## 2. Enable systemd user lingering (one-time)

So services keep running after you log out and across reboots:

```bash
sudo loginctl enable-linger $USER
```

## 3. Per-source credential setup

You only need creds for the sources you want to sync. Skip sections that don't
apply.

### 3a. Email + OneNote (Microsoft Graph, shared Azure app)

Both email-mcp and onenote-mcp use Microsoft Graph and share one Azure app
registration. Set this up once.

1. Sign in at https://portal.azure.com with the Microsoft 365 / work account
   you want to read mail from.
2. **App registrations** → **New registration**:
   - Name: anything (e.g. `personal-mcp`)
   - Supported account types: "Accounts in this organizational directory only"
   - Leave redirect URI blank
3. After creation, copy the **Application (client) ID** and **Directory
   (tenant) ID** from the Overview page.
4. **API permissions** → **Add a permission** → **Microsoft Graph** →
   **Delegated permissions**. Add:
   - For email-mcp: `Mail.Read`, `Mail.ReadWrite`
   - For onenote-mcp: `Notes.Read`, `Notes.Read.All` (read shared notebooks),
     `Notes.ReadWrite` (write own notebooks), `Notes.Create`
   - **Skip `offline_access`** — MSAL adds it internally and rejects it in the
     scope list.
   - Optionally add `Notes.ReadWrite.All` if you want to write to shared
     notebooks (rarely needed).
5. Click **Grant admin consent** — your tenant may require admin approval; if
   so, ask IT.
6. **Authentication** → **Add a platform** → **Mobile and desktop applications**
   → check `https://login.microsoftonline.com/common/oauth2/nativeclient`.
7. Set environment variables (add to `~/.profile` or your shell rc):
   ```bash
   export EMAIL_MCP_CLIENT_ID=<your-client-id>
   export EMAIL_MCP_TENANT_ID=<your-tenant-id>
   export ONENOTE_MCP_CLIENT_ID=<your-client-id>     # same app
   export ONENOTE_MCP_TENANT_ID=<your-tenant-id>
   ```
   Alternatively, paste them into the corresponding systemd unit files.
8. First run: launch each service interactively to complete the device-code
   flow:
   ```bash
   ~/projects/email-mcp/venv/bin/python ~/projects/email-mcp/main.py
   ```
   A code will print to stdout. Open the URL on any device, sign in, paste the
   code. Repeat for onenote-mcp (separate token cache, even though same app).
9. After both first-run sign-ins succeed, hit Ctrl-C, then start them as
   services:
   ```bash
   systemctl --user start email-mcp.service onenote-mcp.service
   ```

> **OneNote 5,000-item caveat.** If your OneDrive has > 5,000 OneNote items
> (notebooks + sections + section groups), Microsoft Graph returns HTTP 403
> error 10008 on `/me/onenote/notebooks/{id}/sections`. onenote-mcp works
> around this by using the flat `/me/onenote/sections` endpoint with parent
> expansion. Some content may still be inaccessible — old Evernote-import
> notebooks are common offenders.

### 3b. ClickUp

1. Open ClickUp → click your avatar (bottom-left) → **Settings** → **Apps**
2. Under **API Token**, click **Generate** (or copy existing)
3. The token starts with `pk_…`
4. First run will prompt for the token; paste it. It's saved to
   `~/.clickup-mcp/config.json` (chmod 600).

```bash
~/projects/clickup-mcp/venv/bin/python ~/projects/clickup-mcp/main.py
```

### 3c. Granola (Enterprise plan required)

1. Open Granola → **Settings** → **Workspaces** → **API** tab → **Generate API
   Key**. Key starts with `sk_…`.
2. First run prompts for the key; saved to `~/.granola-mcp/config.json`.

```bash
~/projects/granola-mcp/venv/bin/python ~/projects/granola-mcp/main.py
```

### 3d. Slack

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch** →
   pick your workspace
2. **OAuth & Permissions** → scroll down to **User Token Scopes** (NOT Bot
   Token Scopes — they're different boxes on the same page).
3. Add these user scopes:
   ```
   channels:read    channels:history
   groups:read      groups:history
   im:read          im:history
   mpim:read        mpim:history
   users:read       users:read.email
   files:read       team:read        reactions:read
   ```
4. Scroll back up → **Install to Workspace** → approve. Your workspace admin
   may need to approve.
5. After install, the page shows a **User OAuth Token** starting with `xoxp-…`.
   Copy it.
6. First run prompts for the token; saved to `~/.slack-mcp/config.json`.

```bash
~/projects/slack-mcp/venv/bin/python ~/projects/slack-mcp/main.py
```

## 4. Initial sync

The first run of each service does a full sync of historical data:

- email-mcp: minutes to hours depending on mailbox size (the full sync is
  resumable across restarts — track which folders are done in the DB).
- slack-mcp: ~5–30 min depending on workspace size; slowest because of
  Slack's per-tier rate limits.
- clickup-mcp: fast (minutes).
- granola-mcp: fast (metadata only; transcripts fetched lazily on first access).
- onenote-mcp: fast (page metadata only; bodies fetched lazily).

Watch progress:
```bash
tail -f ~/.email-mcp/sync.log
```

## 5. Chat UI setup

Once at least one sync MCP is up, configure the chat UI.

### 5a. LLM endpoint

Pick one of:

**Local Ollama (recommended for privacy):**
```bash
# Install Ollama from https://ollama.com
ollama pull qwen3:32b      # ~17GB. Use a different model if you prefer.
```

**Remote OpenAI-compatible endpoint:**
- Any vLLM/LiteLLM/OpenRouter/OpenAI endpoint works. You need the base URL and
  an API key.

### 5b. Configure chat-mcp

Create `~/.chat-mcp/config.json`:

```json
{
  "llm": {
    "endpoint": "http://127.0.0.1:11434",
    "api_key": "ollama",
    "model": "qwen3:32b",
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

Only include the `mcp_servers` entries for sources you actually ran. Set
`bind.host` to `127.0.0.1` if you want localhost-only, or `0.0.0.0` to expose
it on the LAN (combine with a firewall or Tailscale ACLs).

### 5c. Personalize the system prompt (optional)

By default the chat agent has a generic system prompt. If you want it to know
your name, role, calendar handle, etc., create
`~/.chat-mcp/system_prompt.md` — chat-mcp loads it instead of the default.
See `chat-mcp/chat/default_system_prompt.md` for the template.

### 5d. Start

```bash
systemctl --user start chat-mcp.service
```

Open `http://<your-host>:8082/` in any browser on your network.

## 6. Verification

```bash
./health_check.sh   # ← from the repo root on your host
```

You should see all enabled services `active`, all expected ports listening,
and recent data freshness per source.

## 7. Optional: HTTPS + auth

The default setup binds the chat UI in plaintext on the LAN. If you want HTTPS
or stronger auth:

- **Tailscale Serve** (easiest, free cert): `tailscale serve --bg --https=443
  http://127.0.0.1:8082`. Then visit `https://<host>.<tailnet>.ts.net/`.
- **Caddy reverse proxy** with a `tailscale cert` if you want multiple
  subdomains/paths or non-Tailscale TLS.
- **App-level password** — uncomment/rewrite the dependency in
  `chat-mcp/chat/auth.py`. The default is a no-op since the deployment is
  meant to be on a trusted network.

## Troubleshooting

See **`OPERATIONS.md`** for the full known-issues catalog. Common stumbles:

- **"Tool surface looks broken"** — make sure all MCPs you reference in
  `mcp_servers` are actually running (`systemctl --user is-active`).
- **MSAL device-code "code expired"** — codes have a ~15 min TTL. Re-run
  `main.py` for a fresh code if you stepped away.
- **Slack "Install to Workspace" greyed out** — your workspace admin has
  restricted user-app installs. Either ask for approval or use a workspace
  where you have install rights.
- **OneNote returns no sections** — see OneNote 5,000-item caveat above.
- **Ollama crashes loading a specific model** — try a different model. We saw
  `qwen3-coder:30b` crash on GB10 hardware while `qwen3:32b` worked fine.
- **`chat-mcp` starts before sync-MCPs are ready** — restart chat-mcp once
  everything else is up, or let the reconnect-on-call path handle it.
