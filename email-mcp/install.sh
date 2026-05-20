#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/venv"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_NAME="email-mcp.service"

echo "=== Email MCP Server Installer ==="
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
mkdir -p "$HOME/.email-mcp"
chmod 700 "$HOME/.email-mcp"

# ── systemd user service ──────────────────────────────────────────────────────
echo "Installing systemd user service..."
mkdir -p "$SERVICE_DIR"
cp "$PROJECT_DIR/email-mcp.service" "$SERVICE_DIR/$SERVICE_NAME"

echo ""
echo "┌─────────────────────────────────────────────────────────────┐"
echo "│  IMPORTANT: Edit the service file before enabling it        │"
echo "│                                                             │"
echo "│  Set EMAIL_MCP_CLIENT_ID= to your Azure app client ID:     │"
echo "│                                                             │"
echo "│    \$EDITOR $SERVICE_DIR/$SERVICE_NAME"
echo "│                                                             │"
echo "│  Then enable and start:                                     │"
echo "│    systemctl --user daemon-reload                           │"
echo "│    systemctl --user enable --now $SERVICE_NAME              │"
echo "│    systemctl --user status $SERVICE_NAME                    │"
echo "└─────────────────────────────────────────────────────────────┘"
echo ""
echo "Or run directly (after setting EMAIL_MCP_CLIENT_ID):"
echo "  export EMAIL_MCP_CLIENT_ID=<your-client-id>"
echo "  $VENV/bin/python $PROJECT_DIR/main.py"
echo ""
