#!/usr/bin/env bash
# Health + freshness check for the personal-knowledge MCP stack.
# Reports per-service: systemd state, listening port, DB freshness, today's
# sync activity. Safe to run repeatedly; read-only.

set +e
TODAY=$(date +%Y-%m-%d)

declare -A PORT=(
  [email-mcp]=8765
  [clickup-mcp]=8767
  [granola-mcp]=8768
  [onenote-mcp]=8769
  [slack-mcp]=8770
  [chat-mcp]=8082
)

declare -A DBPATH=(
  [email]=~/.email-mcp/mail.db
  [clickup]=~/.clickup-mcp/clickup.db
  [granola]=~/.granola-mcp/granola.db
  [onenote]=~/.onenote-mcp/onenote.db
  [slack]=~/.slack-mcp/slack.db
)

echo "═══════ SERVICE STATE ═══════"
for svc in email-mcp clickup-mcp granola-mcp onenote-mcp slack-mcp chat-mcp; do
  if ! systemctl --user list-unit-files "$svc.service" >/dev/null 2>&1; then
    printf "  %-12s (not installed)\n" "$svc"
    continue
  fi
  active=$(systemctl --user is-active $svc 2>&1)
  pid=$(systemctl --user show $svc -p MainPID --value 2>/dev/null)
  age=$(ps -p "$pid" -o etime --no-headers 2>/dev/null | xargs)
  restarts=$(systemctl --user show $svc -p NRestarts --value 2>/dev/null)
  printf "  %-12s %-8s pid=%-7s age=%-12s restarts=%s\n" \
    "$svc" "$active" "${pid:-?}" "${age:-?}" "${restarts:-?}"
done

echo ""
echo "═══════ PORTS ═══════"
for svc in "${!PORT[@]}"; do
  port=${PORT[$svc]}
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:$port/healthz 2>/dev/null)
  [[ "$code" == "000" || -z "$code" ]] && code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:$port/ 2>/dev/null)
  printf "  %-12s :%-5s HTTP %s\n" "$svc" "$port" "${code:-down}"
done

echo ""
echo "═══════ DATA FRESHNESS ═══════"

for src in email slack clickup granola onenote; do
  db=${DBPATH[$src]}
  if [[ ! -f $db ]]; then
    printf "  %-9s (no db)\n" "$src"
    continue
  fi
  echo "--- $src ---"
  case "$src" in
    email)
      sqlite3 "$db" <<'SQL'
SELECT '  newest received:      ' || COALESCE(MAX(received_datetime), '?') FROM messages;
SELECT '  total messages:       ' || COUNT(*) FROM messages;
SELECT '  msgs today:           ' || COUNT(*) FROM messages WHERE date(received_datetime) = date('now');
SQL
      ;;
    slack)
      sqlite3 "$db" <<'SQL'
SELECT '  newest message UTC:   ' || COALESCE(datetime(MAX(CAST(ts AS REAL)),'unixepoch'), '?') FROM messages;
SELECT '  total messages:       ' || COUNT(*) FROM messages;
SELECT '  msgs today:           ' || COUNT(*) FROM messages WHERE date(CAST(ts AS REAL),'unixepoch') = date('now');
SQL
      ;;
    clickup)
      sqlite3 "$db" <<'SQL'
SELECT '  most recent update:   ' || COALESCE(datetime(MAX(CAST(date_updated AS INTEGER))/1000,'unixepoch'), '?') FROM tasks;
SELECT '  total tasks:          ' || COUNT(*) FROM tasks;
SELECT '  tasks updated today:  ' || COUNT(*) FROM tasks WHERE date(CAST(date_updated AS INTEGER)/1000,'unixepoch') = date('now');
SQL
      ;;
    granola)
      sqlite3 "$db" <<'SQL'
SELECT '  newest updated_at:    ' || COALESCE(MAX(updated_at), '?') FROM notes;
SELECT '  total notes:          ' || COUNT(*) FROM notes;
SELECT '  notes updated today:  ' || COUNT(*) FROM notes WHERE date(updated_at) = date('now');
SQL
      ;;
    onenote)
      sqlite3 "$db" <<'SQL'
SELECT '  newest page modified: ' || COALESCE(MAX(modified_at), '?') FROM pages;
SELECT '  total pages:          ' || COUNT(*) FROM pages;
SELECT '  total sections:       ' || COUNT(*) FROM sections;
SELECT '  pages modified today: ' || COUNT(*) FROM pages WHERE date(modified_at) = date('now');
SQL
      ;;
  esac
done

echo ""
echo "═══════ TODAY'S SYNC LOG ACTIVITY ═══════"
for svc in email clickup granola onenote slack; do
  log=~/.${svc}-mcp/sync.log
  if [[ ! -f $log ]]; then
    printf "  %-9s (no log)\n" "$svc"
    continue
  fi
  lines=$(grep -c "^\[$TODAY" "$log" 2>/dev/null || echo 0)
  errs=$(grep "^\[$TODAY" "$log" 2>/dev/null | grep -iEc "error|fail|invalid|denied|forbidden" || echo 0)
  last=$(grep "^\[$TODAY" "$log" 2>/dev/null | tail -1)
  printf "  %-9s %3d events today, %d errors. Last: %s\n" "$svc" "$lines" "$errs" "${last:--}"
done
