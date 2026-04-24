# Getting this on GitHub

One-time setup to publish the repo and enable CI.

## 1. Create the repo on GitHub

Go to https://github.com/new

- Repository name: `Kalashi_Program` (or whatever you want — update the URL in `pyproject.toml` to match)
- Description: "Prediction market trading bot (Kalshi + Polymarket)"
- Visibility: **Private** initially. Flip to public only when you're ready for the world to see your strategy code.
- Do NOT initialize with README, .gitignore, or license — we already have those locally.

Copy the repo URL GitHub shows you (looks like `git@github.com:YOUR_USERNAME/Kalashi_Program.git`).

## 2. Clean up the OneDrive cache artifacts

Before the first commit, get rid of junk that shouldn't be in git:

```powershell
# From the project root
Remove-Item -Recurse -Force .mypy_cache -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force .pytest_cache -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force __pycache__ -ErrorAction SilentlyContinue
```

The `.gitignore` will keep these out going forward.

## 3. Initialize and push

```powershell
cd C:\Users\Drama\OneDrive\Kalashi_Program

# Initialize
git init -b main
git add .
git status                 # eyeball this — make sure no .env, no .pem, no data/

git commit -m "feat: initial v0.2.0 — pm_bot scaffolding + v1 scanner"

# Hook up remote
git remote add origin git@github.com:YOUR_USERNAME/Kalashi_Program.git
git branch -M main
git push -u origin main

# Create develop branch for ongoing work
git checkout -b develop
git push -u origin develop
```

## 4. Protect main branch

On GitHub: **Settings -> Branches -> Add branch protection rule**.

Recommended rules for `main`:
- Require a pull request before merging
- Require status checks to pass before merging
  - Require `Lint (ruff + black)`
  - Require `Tests (3.11)`
- Require branches to be up to date before merging
- Do not allow force pushes

This means you can't accidentally push broken code to main — CI has to be green first.

## 5. Verify CI

Push a trivial commit to `develop`:

```powershell
git checkout develop
echo "" >> README_PM_BOT.md    # no-op change
git add README_PM_BOT.md
git commit -m "chore: trigger CI"
git push
```

Go to the **Actions** tab on GitHub. You should see the CI workflow run:
- Lint job (may fail initially — fix by running `ruff check --fix` and `black .` locally)
- Type check (expected to fail — relaxed for now)
- Tests (should pass on Python 3.10, 3.11, 3.12)
- Security scan

## 6. (Later) Set up secrets for the release workflow

When you're ready to publish versioned releases:

**Settings -> Secrets and variables -> Actions -> New repository secret**

No secrets are strictly required yet — the release workflow builds and attaches
artifacts without publishing anywhere. If you later decide to publish to PyPI,
you'll add `PYPI_API_TOKEN` here.

## 7. (Later) Deployment to VPS

When you're ready to run on a VPS, the flow becomes:

```bash
# On the VPS:
git clone git@github.com:YOUR_USERNAME/Kalashi_Program.git
cd Kalashi_Program
python -m venv .venv && source .venv/bin/activate
pip install -e .
# Copy over your .env and kalshi_private.pem — NOT via git, via scp
scp ~/local/.env user@vps:/opt/kalashi/.env
scp ~/local/kalshi_private.pem user@vps:/opt/kalashi/kalshi_private.pem
chmod 600 /opt/kalashi/.env /opt/kalashi/kalshi_private.pem

# Install as systemd service (provided below)
sudo cp deploy/pm_bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pm_bot
```

We'll add `deploy/pm_bot.service` when you're ready for that step.

## Common snags

**Authentication failure on push.** GitHub deprecated password auth. Use an SSH key
(preferred) or a Personal Access Token. SSH setup: https://docs.github.com/en/authentication/connecting-to-github-with-ssh

**`.mypy_cache` or `.pytest_cache` showing up in first commit.** They're in
`.gitignore` but if they were already tracked, remove them:

```powershell
git rm -rf --cached .mypy_cache .pytest_cache
git commit -m "chore: remove cache from tracking"
```

**OneDrive sync conflicts.** OneDrive can lock files mid-edit, which makes
`git add` complain intermittently. If it happens: pause OneDrive sync, commit,
resume. Long term, consider moving the repo out of the OneDrive folder
(`C:\code\Kalashi_Program` instead) — OneDrive and git don't love each other.
