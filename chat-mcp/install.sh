#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/venv"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_NAME="chat-mcp.service"

echo "=== chat-mcp Installer ==="
echo ""

# ── Python venv ───────────────────────────────────────────────────────────────
if [[ ! -d "$VENV" ]]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV"
fi

echo "Installing Python dependencies..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"
echo "Dependencies installed."
echo ""

# ── Config dir ────────────────────────────────────────────────────────────────
mkdir -p "$HOME/.chat-mcp"
chmod 700 "$HOME/.chat-mcp"

if [[ ! -f "$HOME/.chat-mcp/config.json" ]]; then
    echo "WARNING: ~/.chat-mcp/config.json not found. Create it before starting."
fi

# ── systemd user service ──────────────────────────────────────────────────────
mkdir -p "$SERVICE_DIR"
cp "$PROJECT_DIR/chat-mcp.service" "$SERVICE_DIR/$SERVICE_NAME"
systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"

cat <<EOF

┌─────────────────────────────────────────────────────────────────┐
│  chat-mcp service enabled (NOT started)                          │
│                                                                  │
│  Start with:                                                     │
│    systemctl --user start chat-mcp.service                       │
│    systemctl --user status chat-mcp.service                      │
│                                                                  │
│  Then expose over Tailscale (run as your user, NOT root):        │
│    tailscale serve --bg --https=443 http://127.0.0.1:8080        │
│                                                                  │
│  Or for testing on http first:                                   │
│    tailscale serve --bg http://127.0.0.1:8080                    │
│                                                                  │
│  The chat UI will then be at:                                    │
│    https://<host>.<your-tailnet>.ts.net/                       │
│                                                                  │
│  Check tailscale serve status:                                   │
│    tailscale serve status                                        │
│                                                                  │
│  Logs:                                                           │
│    tail -f ~/.chat-mcp/chat.log                                  │
└─────────────────────────────────────────────────────────────────┘
EOF
