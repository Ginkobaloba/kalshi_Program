#!/usr/bin/env bash
# ============================================================================
# healthcheck.sh — verify pm_bot is alive and behaving
#
# Returns 0 if healthy, non-zero on failure.
#
# Checks:
#   1. systemd reports the service as 'active'
#   2. The process has been alive for at least 10s (didn't immediately crash)
#   3. The log file has been written to in the last 5 minutes
#   4. The SQLite database is reachable and readable
#   5. No 'kill_switch' event in the events table in the last 10 minutes
#
# Run manually:
#   bash deploy/healthcheck.sh
#
# Or via systemd timer (cron-style):
#   systemctl edit pm_bot_healthcheck.timer
# ============================================================================

set -e

INSTALL_DIR="${INSTALL_DIR:-/opt/kalshi_program}"
SERVICE_NAME="pm_bot"
LOG_FILE="$INSTALL_DIR/logs/pm_bot.log"
DB_FILE="$INSTALL_DIR/data/pm_bot.db"

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

echo "==> Health check for $SERVICE_NAME"

# 1. systemd active
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    fail "systemd reports service inactive"
fi
echo "    [ok] service active"

# 2. uptime > 10s
UPTIME=$(systemctl show "$SERVICE_NAME" --property=ActiveEnterTimestampMonotonic --value)
NOW_MONO=$(awk '{print $1}' /proc/uptime | tr -d '.')
START_MONO=$((UPTIME / 1000000))
ALIVE_FOR=$((NOW_MONO - START_MONO))
if [[ $ALIVE_FOR -lt 10 ]]; then
    fail "process alive only ${ALIVE_FOR}s — likely crashing on startup"
fi
echo "    [ok] alive for ${ALIVE_FOR}s"

# 3. log file freshness (last write within 5 minutes)
if [[ ! -f "$LOG_FILE" ]]; then
    fail "log file not found: $LOG_FILE"
fi
LAST_WRITE=$(stat -c %Y "$LOG_FILE")
NOW=$(date +%s)
AGE=$((NOW - LAST_WRITE))
if [[ $AGE -gt 300 ]]; then
    fail "log file stale: ${AGE}s since last write (limit 300s)"
fi
echo "    [ok] log fresh (${AGE}s old)"

# 4. SQLite database readable
if [[ ! -f "$DB_FILE" ]]; then
    fail "DB file not found: $DB_FILE"
fi
if ! sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM events;" > /dev/null 2>&1; then
    fail "cannot query database"
fi
echo "    [ok] db reachable"

# 5. No kill_switch event in last 10 minutes
KILL_COUNT=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM events WHERE kind='kill_switch' AND ts > datetime('now', '-10 minutes');" 2>/dev/null || echo "0")
if [[ "$KILL_COUNT" != "0" ]]; then
    fail "$KILL_COUNT kill_switch event(s) in last 10 minutes"
fi
echo "    [ok] no kill switch triggers"

echo "==> healthy"
exit 0
