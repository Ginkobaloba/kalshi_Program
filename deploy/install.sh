#!/usr/bin/env bash
# ============================================================================
# install.sh — one-time VPS bootstrap for pm_bot
#
# Run AS ROOT on a fresh Ubuntu 22.04+ VPS:
#   curl -sSL https://raw.githubusercontent.com/Ginkobaloba/kalshi_program/main/deploy/install.sh | sudo bash
# Or, after cloning:
#   sudo bash deploy/install.sh
#
# This:
#   - Creates a 'pmbot' system user (no shell, no home dir login)
#   - Installs Python 3.11+, git, sqlite3
#   - Clones the repo to /opt/kalshi_program
#   - Sets up a Python venv with deps
#   - Installs the systemd unit (but does NOT start it — you need .env first)
#   - Sets up logrotate
#
# After running this, you still need to:
#   1. scp your .env file to /opt/kalshi_program/.env
#   2. scp your kalshi_private.pem to /opt/kalshi_program/kalshi_private.pem
#   3. Verify config.yaml has paper_trading: true (NEVER deploy live first)
#   4. systemctl start pm_bot
#   5. journalctl -u pm_bot -f  (watch for clean startup)
# ============================================================================

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root (use sudo)" >&2
   exit 1
fi

REPO_URL="${REPO_URL:-https://github.com/Ginkobaloba/kalshi_program.git}"
INSTALL_DIR="/opt/kalshi_program"
SERVICE_USER="pmbot"

echo "==> [1/7] Updating apt and installing prerequisites..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git sqlite3 curl logrotate

echo "==> [2/7] Creating system user '$SERVICE_USER'..."
if ! id -u "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> [3/7] Cloning repo to $INSTALL_DIR..."
if [[ -d "$INSTALL_DIR/.git" ]]; then
    echo "    repo already exists; pulling latest"
    su -s /bin/bash -c "cd $INSTALL_DIR && git pull origin main" "$SERVICE_USER" || true
else
    mkdir -p "$INSTALL_DIR"
    chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
    su -s /bin/bash -c "git clone $REPO_URL $INSTALL_DIR" "$SERVICE_USER"
fi

echo "==> [4/7] Setting up Python venv and installing deps..."
su -s /bin/bash -c "cd $INSTALL_DIR && python3 -m venv .venv && .venv/bin/pip install --quiet --upgrade pip && .venv/bin/pip install --quiet -e ." "$SERVICE_USER"

echo "==> [5/7] Creating data and logs dirs..."
mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/logs"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/data" "$INSTALL_DIR/logs"

echo "==> [6/7] Installing systemd unit..."
cp "$INSTALL_DIR/deploy/pm_bot.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable pm_bot
echo "    service enabled (will start on boot, but NOT started yet)"

echo "==> [7/7] Setting up log rotation..."
cat > /etc/logrotate.d/pm_bot <<LROT
$INSTALL_DIR/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    su $SERVICE_USER $SERVICE_USER
}
LROT

echo ""
echo "============================================================"
echo "  pm_bot installed at $INSTALL_DIR"
echo "============================================================"
echo ""
echo "  NEXT STEPS (you must do these manually):"
echo ""
echo "  1. Copy your .env file to the VPS:"
echo "       scp .env user@vps:/tmp/.env"
echo "       sudo mv /tmp/.env $INSTALL_DIR/.env"
echo "       sudo chown $SERVICE_USER:$SERVICE_USER $INSTALL_DIR/.env"
echo "       sudo chmod 600 $INSTALL_DIR/.env"
echo ""
echo "  2. Copy your Kalshi private key:"
echo "       scp kalshi_private.pem user@vps:/tmp/kalshi_private.pem"
echo "       sudo mv /tmp/kalshi_private.pem $INSTALL_DIR/"
echo "       sudo chown $SERVICE_USER:$SERVICE_USER $INSTALL_DIR/kalshi_private.pem"
echo "       sudo chmod 600 $INSTALL_DIR/kalshi_private.pem"
echo ""
echo "  3. CONFIRM paper_trading is true in config.yaml:"
echo "       sudo grep paper_trading $INSTALL_DIR/config.yaml"
echo ""
echo "  4. Start the bot:"
echo "       sudo systemctl start pm_bot"
echo ""
echo "  5. Watch it boot:"
echo "       sudo journalctl -u pm_bot -f"
echo ""
