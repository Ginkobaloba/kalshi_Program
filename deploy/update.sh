#!/usr/bin/env bash
# ============================================================================
# update.sh — called by GitHub Actions to deploy a new version
#
# This is INTENTIONALLY simple — pull, install deps, restart.
# More sophisticated rollout (blue-green, etc.) is overkill at this scale.
#
# What it does:
#   1. Snapshots the current commit hash for rollback
#   2. Pulls latest from main
#   3. Runs pip install (only re-installs if requirements changed)
#   4. Verifies the bot can at least IMPORT — refuses to deploy on import error
#   5. Restarts the systemd service
#   6. Waits 15s, runs healthcheck
#   7. If healthcheck fails: rolls back automatically
# ============================================================================

set -euo pipefail

INSTALL_DIR="/opt/kalshi_program"
SERVICE_NAME="pm_bot"

cd "$INSTALL_DIR"

echo "==> Snapshotting current revision for rollback..."
PREV_REV=$(git rev-parse HEAD)
echo "    previous: $PREV_REV"

echo "==> Pulling latest from main..."
git fetch origin main
git reset --hard origin/main
NEW_REV=$(git rev-parse HEAD)
echo "    new: $NEW_REV"

if [[ "$PREV_REV" == "$NEW_REV" ]]; then
    echo "==> No changes to deploy"
    exit 0
fi

echo "==> Installing dependencies..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e .

echo "==> Pre-flight: verifying package imports..."
if ! .venv/bin/python -c "import pm_bot; from pm_bot.exchanges.kalshi import KalshiAdapter; from pm_bot.strategies.cross_market_arb import CrossMarketArbStrategy"; then
    echo "    FAIL: import check failed. Rolling back to $PREV_REV"
    git reset --hard "$PREV_REV"
    .venv/bin/pip install --quiet -e .
    exit 1
fi

echo "==> Pre-flight: smoke testing one dry scan cycle..."
if ! timeout 60 .venv/bin/python run_bot.py --once --dry; then
    echo "    FAIL: smoke test crashed. Rolling back to $PREV_REV"
    git reset --hard "$PREV_REV"
    .venv/bin/pip install --quiet -e .
    exit 1
fi

echo "==> Restarting systemd service..."
systemctl restart "$SERVICE_NAME"

echo "==> Waiting 15s for service to stabilize..."
sleep 15

echo "==> Running health check..."
if ! bash "$INSTALL_DIR/deploy/healthcheck.sh"; then
    echo "    FAIL: post-deploy health check failed. Rolling back."
    git reset --hard "$PREV_REV"
    .venv/bin/pip install --quiet -e .
    systemctl restart "$SERVICE_NAME"
    sleep 10
    exit 1
fi

echo ""
echo "==> Deploy successful: $PREV_REV -> $NEW_REV"
echo ""
