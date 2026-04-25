# Deploy runbook

How to set up and operate `pm_bot` on a VPS with continuous deployment.

## Why this is set up the way it is

Trading bots are not web apps. The standard "merge to main → auto-deploy to prod" pattern would mean a bug merged at 3am gets deployed to your live trading account at 3am, which is exactly when you're not awake to catch it.

This setup uses a **two-stage deploy with paper-first**:

1. Merge to `main` → CI runs → **auto-deploys to PAPER VPS**
2. Paper bot runs for hours/days; you watch the SQLite trade log accumulate
3. When you're satisfied, **manually trigger** deploy to LIVE VPS via GitHub Actions
4. Both stages run smoke tests + health checks; auto-rollback on failure

## Architecture

```
GitHub                                VPS (paper)
  ┌─────────────┐    push to main       ┌─────────────────────┐
  │  main branch│ ───────────────────► │ /opt/kalshi_program │
  │             │                        │ paper_trading: true │
  └─────────────┘                        │ ↑ systemd service   │
        │                                └─────────────────────┘
        │ workflow_dispatch (manual)
        │
        ▼                                VPS (live, optional)
                                         ┌─────────────────────┐
                                         │ /opt/kalshi_program │
                                         │ paper_trading: false│
                                         │ ↑ systemd service   │
                                         └─────────────────────┘
```

You can run paper-only on one VPS for now (recommended for first 1-3 months). Add the live VPS only when paper is showing consistent edge.

## One-time VPS setup

Pick any Ubuntu 22.04+ VPS. Cheapest options:
- **Hetzner CX22** — €4/mo, EU
- **DigitalOcean basic** — $6/mo, US-East available (closest to Kalshi)
- **AWS Lightsail** — $3.50/mo
- **Linode Nanode** — $5/mo

For US prediction markets, prefer US-East 1 region — minimizes latency to Kalshi (which runs on AWS us-east).

### Bootstrap script

SSH into the fresh VPS as root (or any sudo user):

```bash
# Clone the repo (you'll need your SSH key set up for GitHub)
git clone https://github.com/Ginkobaloba/kalshi_program.git /tmp/kp
sudo bash /tmp/kp/deploy/install.sh
```

The script:
- Creates a `pmbot` system user (no shell login)
- Installs Python 3.10+, git, sqlite3
- Clones the repo to `/opt/kalshi_program`
- Sets up Python venv with deps
- Installs the systemd service (enabled but not started)
- Sets up logrotate

### Copy secrets to the VPS (manual, ONE TIME)

Secrets live ONLY on the VPS, never in the repo:

```bash
# From your local machine:
scp .env user@vps:/tmp/.env
scp kalshi_private.pem user@vps:/tmp/kalshi_private.pem

# Then on the VPS:
sudo mv /tmp/.env /opt/kalshi_program/.env
sudo mv /tmp/kalshi_private.pem /opt/kalshi_program/kalshi_private.pem
sudo chown pmbot:pmbot /opt/kalshi_program/.env /opt/kalshi_program/kalshi_private.pem
sudo chmod 600 /opt/kalshi_program/.env /opt/kalshi_program/kalshi_private.pem
```

### Confirm config + start the bot

```bash
# Make sure paper trading is on (NEVER deploy live first)
sudo grep paper_trading /opt/kalshi_program/config.yaml
# Should print: paper_trading: true

# Start the bot
sudo systemctl start pm_bot

# Watch it boot
sudo journalctl -u pm_bot -f
```

You should see "Starting pm_bot | paper=True ..." within a few seconds.

## Setting up GitHub Actions deployment

### 1. Create a deploy user on the VPS

Don't reuse root for SSH-based deploys. Create a dedicated `deploy` user with sudo permission for ONLY the update script:

```bash
sudo useradd -m -s /bin/bash deploy
sudo passwd -d deploy   # no password, ssh-key only
mkdir -p /home/deploy/.ssh
chmod 700 /home/deploy/.ssh

# Generate a deploy SSH key locally:
#   ssh-keygen -t ed25519 -f ~/.ssh/pm_bot_deploy -C "github-deploy"
# Add the PUBLIC key to the VPS:
echo "ssh-ed25519 AAAA... github-deploy" | sudo tee /home/deploy/.ssh/authorized_keys
sudo chown -R deploy:deploy /home/deploy/.ssh
sudo chmod 600 /home/deploy/.ssh/authorized_keys

# Allow deploy user to run ONLY the update script with sudo, no password:
echo "deploy ALL=(ALL) NOPASSWD: /opt/kalshi_program/deploy/update.sh" | sudo tee /etc/sudoers.d/pm_bot_deploy
sudo chmod 440 /etc/sudoers.d/pm_bot_deploy
```

### 2. Add secrets to GitHub

In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Value |
|---|---|
| `PAPER_VPS_HOST` | hostname or IP of the paper VPS |
| `PAPER_VPS_USER` | `deploy` |
| `PAPER_VPS_SSH_KEY` | contents of the PRIVATE key (`~/.ssh/pm_bot_deploy`) |
| `LIVE_VPS_HOST` | (only if you have a separate live VPS) |
| `LIVE_VPS_USER` | (same) |
| `LIVE_VPS_SSH_KEY` | (same) |

### 3. Set up GitHub Environments (for the manual approval gate)

In **Settings → Environments**:

- Create environment `paper` — no protection rules needed
- Create environment `live` — add **Required reviewers** (yourself), so any deploy to live requires you to click "Approve" in the GitHub UI

### 4. Test the workflow

Push a trivial change to `main` (or merge a PR). Watch GitHub Actions:
- CI runs (lint, type check, tests)
- After CI passes, `Deploy → deploy_paper` job runs
- It SSHes to your paper VPS, runs `update.sh`, checks health

If anything fails, check the workflow logs. Common issues:
- SSH key mismatch — re-paste in GitHub secrets
- VPS host key not trusted — workflow does `ssh-keyscan` first; if VPS key changed, manually run keyscan again
- update.sh permission denied — check sudoers config

### 5. Deploy to live (when ready)

Don't do this until paper has been running successfully for at least a week.

In GitHub Actions:
1. Click **Run workflow** on the Deploy workflow
2. Select target: `live`
3. Click **Run workflow**
4. GitHub will pause at "Required reviewers" — go approve yourself
5. Job runs

## Operations

### See what the bot is doing right now

```bash
ssh user@paper-vps
sudo journalctl -u pm_bot -f       # live tail
sudo journalctl -u pm_bot --since "1 hour ago"   # last hour
```

### Inspect the trade database

```bash
sqlite3 /opt/kalshi_program/data/pm_bot.db

sqlite> SELECT strategy, outcome, COUNT(*) FROM signals GROUP BY 1, 2;
sqlite> SELECT * FROM candidates ORDER BY ts DESC LIMIT 10;
sqlite> SELECT * FROM events WHERE kind = 'kill_switch' ORDER BY ts DESC LIMIT 5;
```

### Stop the bot (graceful — cancels open orders)

```bash
sudo systemctl stop pm_bot
```

The systemd unit is configured to send SIGTERM with a 30-second grace period — `run_bot.py` catches SIGTERM and runs cleanup.

### Emergency stop (from anywhere with SSH)

```bash
ssh user@vps "sudo systemctl stop pm_bot"
```

### Roll back to a previous version manually

```bash
ssh user@vps
cd /opt/kalshi_program
sudo -u pmbot git log --oneline | head    # find the commit you want
sudo -u pmbot git reset --hard <commit>
sudo -u pmbot .venv/bin/pip install -e .
sudo systemctl restart pm_bot
```

### Add a kill switch via GitHub

Push a one-line change to a file like `KILLSWITCH` in main with content `STOP`. Add a step to update.sh that checks for this file and refuses to start the bot if it exists. (Not done by default — opt in if you're paranoid.)

## Cost estimate

Single paper VPS at $5-10/mo = $60-120/yr.
At a $1k bankroll, that's 6-12% of bankroll just for infra. Don't add a second VPS for live until your bankroll is closer to $5k OR your paper bot is showing edge.

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `update.sh` exits 1 with "smoke test crashed" | New code has runtime bug not caught by CI | Auto-rolls back; check journal for the traceback |
| Bot keeps restarting | Probably config/env error | `journalctl -u pm_bot -n 50` |
| GitHub Actions deploy hangs | SSH key wrong, or VPS firewall | Test `ssh deploy@vps echo ok` from your machine |
| Bot starts but never trades | Probably `paper_trading: true` (working as intended) OR strategies all disabled | Check `config.yaml`, check signals table |
| Port 22 blocked | Cloud provider firewall | Open port 22 to GitHub Actions IP ranges OR use a static deploy runner |

## Going further (not yet)

When bankroll grows / strategy proves out:
- Add Prometheus metrics + Grafana dashboard
- Add Discord/Slack alerting on kill switches and big P&L moves
- Add a second VPS for redundancy
- Container deploys (Docker + a registry)
- Health check on a cron timer that pages you if the bot dies

But not until you have actual edge to protect. Don't over-engineer the deployment of a strategy that doesn't work yet.
