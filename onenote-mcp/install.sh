#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/venv"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_NAME="onenote-mcp.service"

echo "=== OneNote MCP Server Installer ==="
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
mkdir -p "$HOME/.onenote-mcp"
chmod 700 "$HOME/.onenote-mcp"

# ── systemd user service ──────────────────────────────────────────────────────
echo "Installing systemd user service..."
mkdir -p "$SERVICE_DIR"
cp "$PROJECT_DIR/$SERVICE_NAME" "$SERVICE_DIR/$SERVICE_NAME"

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"

echo ""
echo "Service enabled. After first interactive run completes the device code auth, start with: systemctl --user start onenote-mcp.service"
echo ""
echo "First-run (interactive, to complete device-code auth):"
echo "  $VENV/bin/python $PROJECT_DIR/main.py"
echo ""
