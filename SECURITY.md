# Security Policy

## What this project stores

- **API keys** (Kalshi, Polymarket, FRED, NOAA) — read from `.env` only, never committed.
- **Private key** — Kalshi RSA private key, read from a `.pem` file, never committed.
- **Trade logs** — SQLite database in `./data/`. Contains your trading history. Don't commit, don't share publicly.
- **Operational logs** — in `./logs/`. May contain redacted API responses. Don't commit.

## If you leak a key

1. Kalshi: rotate immediately at https://kalshi.com/settings/api-keys — revoke the old key, generate a new one.
2. Polymarket: rotate via the CLOB API key management flow.
3. Commit the fix to `.gitignore` or the code that leaked it.
4. `git filter-repo` or BFG to scrub from history if the commit went public.
5. Force-push only if you're sure no one else has the repo cloned.

## Reporting a vulnerability

This is a personal research project, no formal SLA. If you find something
that could cause someone to lose money, open a GitHub issue with the
`security` label or email dramattick1@gmail.com.

Please DO NOT exploit or disclose publicly before giving a reasonable
chance to fix.

## Dependencies

CI runs `pip-audit` on every push. Check the Actions tab for CVE alerts.

Dependabot is configured to open PRs for dep updates weekly. Review them,
merge the non-breaking ones promptly.
