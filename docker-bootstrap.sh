#!/usr/bin/env bash
# Interactive first-run installer for the docker compose path.
# Mirrors bootstrap.sh (which handles the systemd path).
#
# Walks the user through:
#   1. .env: Azure CLIENT_ID/TENANT_ID for email + onenote (Microsoft 365)
#   2. docker compose build
#   3. Per-MCP first-time auth (device-code or paste-a-token), one at a time
#   4. chat-mcp config.json written into the chat-data volume
#   5. Pull the LLM model into the bundled ollama container
#   6. docker compose up -d

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

# ── ui helpers ────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; BLUE=$'\033[34m'; YELLOW=$'\033[33m'
  GREEN=$'\033[32m'; RED=$'\033[31m'; OFF=$'\033[0m'
else
  BOLD=""; BLUE=""; YELLOW=""; GREEN=""; RED=""; OFF=""
fi

step() { printf "\n${BOLD}${BLUE}══ %s ══${OFF}\n" "$*"; }
ok()   { printf "${GREEN}✓${OFF} %s\n" "$*"; }
warn() { printf "${YELLOW}⚠${OFF} %s\n" "$*"; }
err()  { printf "${RED}✗${OFF} %s\n" "$*" >&2; }

ask_yn() {
  local prompt=$1 default=${2:-N} reply
  local hint="[y/N]"; [[ "$default" == "Y" ]] && hint="[Y/n]"
  printf "${YELLOW}?${OFF} %s %s " "$prompt" "$hint"
  read -r reply
  reply=${reply:-$default}
  [[ "$reply" =~ ^[Yy] ]]
}

ask_value() {
  # ask_value VAR "prompt"  → reads into VAR, no default
  local __var=$1 prompt=$2 reply
  printf "${YELLOW}?${OFF} %s " "$prompt"
  read -r reply
  printf -v "$__var" '%s' "$reply"
}

# ── docker compose wrapper (handles sudo if user isn't in docker group) ───
DC=(docker compose)
if ! docker info >/dev/null 2>&1; then
  if sudo -n docker info >/dev/null 2>&1; then
    DC=(sudo docker compose)
    warn "Using sudo for docker (user not in docker group)."
  else
    err "Cannot reach the docker daemon. Add your user to the docker group, or run this script with sudo."
    exit 1
  fi
fi
dc() { "${DC[@]}" "$@"; }
# Same sudo prefix as compose, but for raw `docker` (volume, run, etc.) — not `docker compose`.
DOCKER=(docker)
[[ "${DC[0]}" == "sudo" ]] && DOCKER=(sudo docker)
dk() { "${DOCKER[@]}" "$@"; }

# ── prereqs ───────────────────────────────────────────────────────────────
step "Checking prerequisites"
command -v docker >/dev/null || { err "docker not installed"; exit 1; }
dc version >/dev/null 2>&1 || { err "docker compose plugin not available"; exit 1; }
ok "docker $(docker --version | awk '{print $3}' | tr -d ',') / compose $(dc version --short)"

# ── security note ─────────────────────────────────────────────────────────
cat <<EOF

${BOLD}${YELLOW}Security reminder${OFF}
The chat UI ships with the permissive ${BOLD}allow_all${OFF} auth stub. It
assumes you reach it over a LAN / Tailscale, not the public internet.
Before publishing the stack, make sure port 8082 (and 8765–8770, 11434)
are NOT exposed to WAN.
EOF
ask_yn "Continue?" Y || exit 0

# ── .env ──────────────────────────────────────────────────────────────────
step "Configuring .env"
if [[ ! -f .env ]]; then
  cp .env.example .env
  ok "Created .env from .env.example"
fi

write_env() {
  local key=$1 val=$2
  if grep -q "^${key}=" .env; then
    # use a delimiter that won't appear in values
    sed -i "s|^${key}=.*|${key}=${val}|" .env
  else
    printf '%s=%s\n' "$key" "$val" >> .env
  fi
}

read_env() { grep "^${1}=" .env | head -1 | cut -d= -f2-; }

prompt_env() {
  # prompt_env KEY "label" — skips if already populated in .env
  local key=$1 label=$2 cur
  cur=$(read_env "$key" || true)
  if [[ -n "$cur" ]]; then
    ok "$key already set (keeping current value)"
    return
  fi
  local val
  ask_value val "  $label"
  [[ -n "$val" ]] && write_env "$key" "$val"
}

ENABLE_MS=false
ENABLE_CLICKUP=false
ENABLE_GRANOLA=false
ENABLE_SLACK=false

cat <<EOF

You only need credentials for the sources you actually want to enable.
Press Enter to skip any prompt; that source will be left unconfigured.
EOF

if ask_yn "Enable email-mcp + onenote-mcp (Microsoft 365)?"; then
  ENABLE_MS=true
  cat <<EOF
  Needs an Azure app registration with delegated permissions
  Mail.Read + Notes.Read.All. See README "Microsoft 365 mail + OneNote"
  for the 2-minute walkthrough. You'll need the app's CLIENT_ID and
  the TENANT_ID it lives in (one Azure app works for both services).
EOF
  prompt_env EMAIL_MCP_CLIENT_ID    "EMAIL_MCP_CLIENT_ID (Azure app client ID):"
  prompt_env EMAIL_MCP_TENANT_ID    "EMAIL_MCP_TENANT_ID (Azure tenant ID):"
  # default onenote to the same app unless user changes it
  if [[ -z "$(read_env ONENOTE_MCP_CLIENT_ID || true)" ]]; then
    write_env ONENOTE_MCP_CLIENT_ID "$(read_env EMAIL_MCP_CLIENT_ID)"
    write_env ONENOTE_MCP_TENANT_ID "$(read_env EMAIL_MCP_TENANT_ID)"
    ok "Reused the same Azure app for ONENOTE_MCP_*"
  fi
fi

ask_yn "Enable clickup-mcp?" && ENABLE_CLICKUP=true
ask_yn "Enable granola-mcp?" && ENABLE_GRANOLA=true
ask_yn "Enable slack-mcp?"   && ENABLE_SLACK=true

# ── build ─────────────────────────────────────────────────────────────────
step "Building images"
dc build

# ── first-time auth ───────────────────────────────────────────────────────
step "Running first-time auth"

auth_one() {
  local svc=$1 hint=$2
  echo
  printf "${BOLD}—— %s ——${OFF}\n" "$svc"
  echo "$hint"
  echo "When auth completes (token saved), press ${BOLD}Ctrl-C${OFF} to exit the container."
  ask_yn "Run interactive auth for $svc now?" Y || { warn "Skipped $svc"; return; }
  dc run --rm "$svc" || warn "$svc exited non-zero (often expected — Ctrl-C after auth)"
}

if $ENABLE_MS;      then auth_one email-mcp   "Microsoft Graph device-code flow. You'll be shown a URL + code to enter in a browser, signed-in as the mailbox user."; fi
if $ENABLE_MS;      then auth_one onenote-mcp "Same device-code flow as email-mcp (often shares cached token)."; fi
if $ENABLE_CLICKUP; then auth_one clickup-mcp "Paste your ClickUp personal API token (starts with pk_…)."; fi
if $ENABLE_GRANOLA; then auth_one granola-mcp "Paste your Granola Enterprise API key (starts with sk_…)."; fi
if $ENABLE_SLACK;   then auth_one slack-mcp   "Paste your Slack User OAuth Token (starts with xoxp-…)."; fi

# ── chat-mcp config ───────────────────────────────────────────────────────
step "Writing chat-mcp config"
LLM_ENDPOINT="http://ollama:11434"
LLM_MODEL="qwen3:32b"
USE_BUNDLED_OLLAMA=true

if ask_yn "Use the bundled Ollama container?" Y; then
  ask_value m "  Model name [$LLM_MODEL]:"; [[ -n "$m" ]] && LLM_MODEL="$m"
else
  USE_BUNDLED_OLLAMA=false
  ask_value LLM_ENDPOINT "  LLM endpoint URL (OpenAI-compatible base, e.g. http://host.docker.internal:11434 or https://api.example.com):"
  ask_value m "  Model name [$LLM_MODEL]:"; [[ -n "$m" ]] && LLM_MODEL="$m"
fi

# Build the mcp_servers map only for services the user enabled
mcp_entries=()
$ENABLE_MS      && mcp_entries+=("\"email\":   \"http://email-mcp:8765/sse\"")
$ENABLE_CLICKUP && mcp_entries+=("\"clickup\": \"http://clickup-mcp:8767/sse\"")
$ENABLE_GRANOLA && mcp_entries+=("\"granola\": \"http://granola-mcp:8768/sse\"")
$ENABLE_MS      && mcp_entries+=("\"onenote\": \"http://onenote-mcp:8769/sse\"")
$ENABLE_SLACK   && mcp_entries+=("\"slack\":   \"http://slack-mcp:8770/sse\"")
mcp_body=$(IFS=,; echo "${mcp_entries[*]}")

tmp=$(mktemp)
cat > "$tmp" <<JSON
{
  "llm": {
    "endpoint": "$LLM_ENDPOINT",
    "api_key": "ollama",
    "model": "$LLM_MODEL",
    "max_tokens": 8000
  },
  "mcp_servers": { $mcp_body },
  "bind": { "host": "0.0.0.0", "port": 8082 },
  "hidden_tools": [
    "email_sync_status", "email_force_sync",
    "clickup_sync_status", "clickup_force_sync", "clickup_get_member",
    "granola_sync_status", "granola_force_sync",
    "onenote_sync_status", "onenote_force_sync",
    "slack_sync_status", "slack_force_sync"
  ]
}
JSON

VOLUME=personal-mcp-stack_chat-data
dk volume create "$VOLUME" >/dev/null
dk run --rm -v "$VOLUME":/data -v "$tmp":/src/config.json --entrypoint sh \
  python:3.12-slim -c 'mkdir -p /data/.chat-mcp && chmod 700 /data/.chat-mcp && cp /src/config.json /data/.chat-mcp/config.json'
rm -f "$tmp"
ok "Wrote config.json to volume $VOLUME"

# ── pull the LLM model ────────────────────────────────────────────────────
if $USE_BUNDLED_OLLAMA; then
  step "Pulling $LLM_MODEL into bundled ollama (this can be several GB)"
  dc up -d ollama
  # Wait for ollama to be reachable
  for i in $(seq 1 30); do
    if dc exec -T ollama ollama list >/dev/null 2>&1; then break; fi
    sleep 1
  done
  dc exec -T ollama ollama pull "$LLM_MODEL"
  ok "Model pulled"
else
  warn "Using external LLM endpoint $LLM_ENDPOINT — make sure $LLM_MODEL is loaded there."
  if [[ "$LLM_ENDPOINT" == *"host.docker.internal"* ]]; then
    cat <<EOF
   Note: on Linux, host-side Ollama (systemd) usually binds 127.0.0.1
   only and is unreachable from docker. To expose it to containers, add
   to /etc/systemd/system/ollama.service.d/override.conf:
       [Service]
       Environment="OLLAMA_HOST=0.0.0.0"
   then: systemctl daemon-reload && systemctl restart ollama
   (Or use the bundled ollama service instead — re-run with default.)
EOF
  fi
fi

# ── up ────────────────────────────────────────────────────────────────────
step "Starting the stack"
SERVICES=()
$ENABLE_MS          && SERVICES+=(email-mcp onenote-mcp)
$ENABLE_CLICKUP     && SERVICES+=(clickup-mcp)
$ENABLE_GRANOLA     && SERVICES+=(granola-mcp)
$ENABLE_SLACK       && SERVICES+=(slack-mcp)
$USE_BUNDLED_OLLAMA && SERVICES+=(ollama)
SERVICES+=(chat-mcp)
# --no-deps so depends_on doesn't drag in services the user opted out of
dc up -d --no-deps "${SERVICES[@]}"

PORT=$(read_env CHAT_MCP_HOST_PORT || true); PORT=${PORT:-8082}
echo
ok "Stack is up. Chat UI: http://localhost:${PORT}/"
echo
warn "Reminder: keep port ${PORT} off the public internet — the chat UI has no auth."
echo
echo "Useful commands:"
echo "  ${DC[*]} ps                # service status"
echo "  ${DC[*]} logs -f chat-mcp  # follow chat-mcp logs"
echo "  ./health_check.sh          # full stack health check (host-side ports)"
