#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/venv"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_NAME="slack-mcp.service"

echo "=== Slack MCP Server Installer ==="
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
mkdir -p "$HOME/.slack-mcp"
chmod 700 "$HOME/.slack-mcp"

# ── systemd user service ──────────────────────────────────────────────────────
echo "Installing systemd user service..."
mkdir -p "$SERVICE_DIR"
cp "$PROJECT_DIR/$SERVICE_NAME" "$SERVICE_DIR/$SERVICE_NAME"
systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
echo "Service $SERVICE_NAME enabled (not yet started)."
echo ""

echo "┌─────────────────────────────────────────────────────────────┐"
echo "│  Next steps                                                 │"
echo "│                                                             │"
echo "│  1. First run (interactive — prompts for OAuth token):     │"
echo "│       $VENV/bin/python $PROJECT_DIR/main.py"
echo "│                                                             │"
echo "│  2. After token saved, start under systemd:                │"
echo "│       systemctl --user start $SERVICE_NAME                  │"
echo "│       systemctl --user status $SERVICE_NAME                 │"
echo "│                                                             │"
echo "│  Logs: tail -f ~/.slack-mcp/sync.log                        │"
echo "└─────────────────────────────────────────────────────────────┘"
echo ""
