# CLAUDE.md — agent setup guide

This file is for Claude Code (or any AI coding assistant) running on the
target host. If you're a human, follow `SETUP.md` instead — this file is
written for an agent driving an interactive install.

## What this project is

Six locally-hosted services that mirror the operator's email, Slack,
ClickUp tasks, Granola meeting notes, and OneNote pages into per-source
SQLite + FTS5 caches, served via MCP, fronted by a local-LLM-backed chat
UI. See `README.md` and `WRITEUP.md` for context. License: MIT.

## Your job

Set up the stack on this machine from a fresh clone. Walk the operator
through each interactive step. Use the existing `bootstrap.sh` and
`health_check.sh` rather than reinventing them. Stop and ask the operator
when you need a credential or a decision — do not invent values.

## Order of operations

1. **Confirm prerequisites with the operator.** Show them this checklist and
   wait for confirmation:
   - Linux (Ubuntu 24.04+ tested) or compatible
   - Python ≥ 3.11
   - `git`, `rsync`, `curl`, `sqlite3` installed
   - ≥ 5 GB free disk (mostly for email)
   - An LLM endpoint — recommend [Ollama](https://ollama.com) locally with
     `qwen3:32b` if they have ≥ 24 GB unified RAM/VRAM; otherwise ask for
     any OpenAI-compatible endpoint URL + key + model name
   - At least one upstream credential ready (see step 5)

2. **Enable user-service lingering** (one-time, requires sudo):
   ```bash
   sudo loginctl enable-linger $USER
   ```
   Ask before running — needs the operator's sudo password.

3. **Ask which sources they want**. Each source is independent. Present:
   - `email-mcp` — Microsoft 365 mail (requires Azure app registration; the
     hardest setup)
   - `slack-mcp` — Slack (requires creating a Slack app + workspace install
     approval)
   - `clickup-mcp` — ClickUp (one-click token from settings)
   - `granola-mcp` — Granola meeting notes (Enterprise plan required; one
     API key)
   - `onenote-mcp` — Microsoft OneNote (reuses the Azure app from `email-mcp`)
   - `chat-mcp` — the web UI (depends on at least one source being installed
     to be useful)

   They can skip any. If they pick only e.g. `slack-mcp` + `chat-mcp`, do
   that and don't install the others.

4. **Run the bootstrap installer**:
   ```bash
   ./bootstrap.sh --only <comma-separated-list>
   # or
   ./bootstrap.sh --all
   ```
   This creates venvs and installs systemd units but does NOT start
   services — credentials come next.

5. **Walk through credentials, one source at a time.** Don't batch — each
   service has interactive steps and gotchas. Cite the exact section of
   `SETUP.md` so the operator can verify your instructions.

   ### email-mcp + onenote-mcp (share an Azure app)
   - Walk them through registering an Azure app at https://portal.azure.com
   - Required delegated Graph permissions:
     - For email: `Mail.Read`, `Mail.ReadWrite`
     - For OneNote: `Notes.Read`, `Notes.Read.All`, `Notes.ReadWrite`,
       `Notes.Create`
     - **Do NOT add `offline_access`** — MSAL rejects it as a reserved scope.
   - Get the **Application (client) ID** and **Directory (tenant) ID**.
   - Edit `~/.config/systemd/user/email-mcp.service` and
     `~/.config/systemd/user/onenote-mcp.service` to set
     `Environment=EMAIL_MCP_CLIENT_ID=…` and `Environment=EMAIL_MCP_TENANT_ID=…`
     (and the equivalent `ONENOTE_MCP_*` lines). Or set them in the
     operator's shell rc.
   - First-run each service interactively for the device-code dance:
     ```bash
     ~/projects/email-mcp/venv/bin/python ~/projects/email-mcp/main.py
     ```
     A code prints to stdout. Operator visits the URL, signs in, pastes
     the code. Ctrl-C once "Authentication successful" appears. Repeat
     for onenote-mcp.
   - Then `systemctl --user start email-mcp onenote-mcp`.

   ### clickup-mcp
   - Operator generates a Personal API Token at ClickUp avatar → Settings
     → Apps → API Token → Generate (starts with `pk_…`).
   - Run interactively: `~/projects/clickup-mcp/venv/bin/python ~/projects/clickup-mcp/main.py`.
   - It prompts for the token; operator pastes it. Then Ctrl-C and
     `systemctl --user start clickup-mcp`.

   ### granola-mcp
   - Requires Granola Enterprise plan.
   - Operator generates the key at Granola → Settings → Workspaces → API
     → Generate (starts with `sk_…`).
   - Same interactive first-run pattern as clickup-mcp.

   ### slack-mcp
   - Walk them through creating a Slack app at https://api.slack.com/apps.
   - Add **User Token Scopes** (NOT Bot Token Scopes — they're different
     boxes on the OAuth & Permissions page). Required: `channels:read`,
     `channels:history`, `groups:read`, `groups:history`, `im:read`,
     `im:history`, `mpim:read`, `mpim:history`, `users:read`,
     `users:read.email`, `files:read`, `team:read`, `reactions:read`.
   - **Install to Workspace** (admin approval may be required at the
     operator's workplace — flag this).
   - Copy the **User OAuth Token** (starts with `xoxp-`).
   - Interactive first-run as above.

6. **First sync will take time.** Email-mcp can take minutes to hours for
   a large mailbox. Slack-mcp can take 5-30 min. Watch with
   `tail -f ~/.<service>-mcp/sync.log`. They're resumable — interrupting
   is safe, the service picks back up.

7. **Configure chat-mcp** (only after at least one sync MCP is up):
   - Create `~/.chat-mcp/config.json`. Ask the operator for:
     - LLM endpoint (e.g. `http://127.0.0.1:11434` for local Ollama)
     - LLM API key (`ollama` for local Ollama, real key for hosted)
     - Model name (e.g. `qwen3:32b`)
   - Only include `mcp_servers` entries for sources they installed.
   - Recommend `hidden_tools` list to filter admin tools from the LLM —
     see `SETUP.md` section 5b for the standard set.
   - Optionally write a personalised `~/.chat-mcp/system_prompt.md`. The
     default template at `chat-mcp/chat/default_system_prompt.md` is fine
     as a starting point; ask the operator for their name, role, and
     usual collaborators if they want it personalised.
   - `systemctl --user start chat-mcp`.

8. **Verify with the health-check script:**
   ```bash
   ./health_check.sh
   ```
   All requested services should show `active`, expected ports HTTP 200,
   per-source data freshness reasonable (most-recently-updated record
   from today, or recently — depending on traffic).

9. **Open the chat UI** at `http://<host>:8082/` and run a smoke test:
   - "How many tasks are open?" — exercises `clickup_count_tasks` (note: not
     yet implemented; will fall back to listing — that's a known limitation
     listed in CONTRIBUTING.md).
   - "Find anything about <a known keyword from their work>" — exercises
     `multi_search`.
   - "What should I work on this week?" — exercises `priority_digest`.

## Common failure modes

If something breaks, check these in order before guessing:

- **`systemctl --user is-active <svc>` shows `failed`** — run
  `journalctl --user -u <svc> -n 50` to see the actual error.
- **Service active but port not listening** — it's still doing initial
  sync. Tail `~/.<svc>-mcp/sync.log`. Wait, don't restart.
- **Email-mcp says `EMAIL_MCP_CLIENT_ID is not set`** — the systemd unit
  has an empty `Environment=` line. Edit the unit, `daemon-reload`,
  restart.
- **OneNote returns no sections** — the operator's OneDrive likely has
  >5,000 OneNote items (the SharePoint list-view ceiling). The flat-endpoint
  workaround in `onenote_mcp/sync.py` will surface what it can; old
  Evernote-import notebooks usually can't be enumerated. Document this
  for the operator; don't try to fix it.
- **Ollama model crashes the runner** — try a different model. `qwen3:32b`
  works on Grace-Blackwell ARM; `qwen3-coder:30b` does not.
- **MSAL device-code expired** — codes have ~15 min TTL. Re-run `main.py`
  for a fresh code if the operator stepped away.
- **Slack "Install to Workspace" greyed out** — workspace admin has
  restricted user-app installs. Operator needs to request admin approval
  or use a workspace where they have install rights.

`OPERATIONS.md` has the full known-issues catalog (14 documented gotchas
with fixes).

## What NOT to do

- Don't paste credentials into chat. Have the operator paste them directly
  into the running `main.py` prompts. The tokens are saved to chmod-600
  files under `~/.<service>-mcp/`.
- Don't commit anything to the cloned repo. The repo is read-only here.
- Don't try to "improve" the existing code on first install — just install
  it. If you spot a bug, surface it to the operator and let them decide.
- Don't use `sudo` except when explicitly required (only for
  `loginctl enable-linger`).

## After install

Hand the operator a summary that includes:

- Which services are running and on which ports
- Where their data lives (`~/.<svc>-mcp/`)
- The chat UI URL
- How to add a model to Ollama if they want to try a different one
- A pointer to `OPERATIONS.md` for ongoing operations
- A pointer to `CONTRIBUTING.md` if they want to add a new source

Then you're done.
