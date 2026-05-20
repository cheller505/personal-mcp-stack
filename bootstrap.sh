#!/usr/bin/env bash
# Bootstrap installer for the personal-knowledge MCP stack.
# Runs each per-service install.sh in order, optionally skipping any source
# the user doesn't want.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

ALL=(email-mcp slack-mcp clickup-mcp granola-mcp onenote-mcp chat-mcp)

usage() {
  cat <<USAGE
Usage: $0 [--all] [--only svc1,svc2,...] [--skip svc1,svc2,...]

Installs venvs and systemd units for the selected MCPs. Does NOT start
them — start manually after providing credentials, see SETUP.md.

Available services: ${ALL[*]}

Examples:
  $0 --all                       # install everything
  $0 --only email-mcp,chat-mcp   # just two
  $0 --skip onenote-mcp          # everything except OneNote
USAGE
}

selection=()
case "${1:-}" in
  --all|"")           selection=("${ALL[@]}") ;;
  --only)             IFS=',' read -ra selection <<< "${2:-}" ;;
  --skip)
    IFS=',' read -ra skip <<< "${2:-}"
    for s in "${ALL[@]}"; do
      keep=true
      for x in "${skip[@]}"; do [[ "$s" == "$x" ]] && keep=false; done
      $keep && selection+=("$s")
    done ;;
  -h|--help)          usage; exit 0 ;;
  *)                  echo "Unknown option: $1"; usage; exit 1 ;;
esac

echo "Will install: ${selection[*]}"
echo

for svc in "${selection[@]}"; do
  if [[ ! -d "$REPO_DIR/$svc" ]]; then
    echo "  skip $svc (directory not present)" >&2
    continue
  fi
  echo "═══ installing $svc ═══"
  ( cd "$REPO_DIR/$svc" && bash install.sh )
  echo
done

echo "═════════════════════════════════════════════════════════════════"
echo "All requested services installed."
echo
echo "Next steps:"
echo "  1. Enable linger so user services survive logout:"
echo "       sudo loginctl enable-linger \$USER"
echo
echo "  2. Provide upstream credentials per service — see SETUP.md."
echo
echo "  3. Interactively start each sync MCP once to complete any auth"
echo "     dance, then switch to systemd:"
echo "       ~/projects/<svc>/venv/bin/python ~/projects/<svc>/main.py"
echo "       # Ctrl-C once auth completes"
echo "       systemctl --user start <svc>.service"
echo
echo "  4. Create ~/.chat-mcp/config.json (see SETUP.md, section 5)."
echo
echo "  5. Verify with: ./health_check.sh"
echo "═════════════════════════════════════════════════════════════════"
