# Contributing

This is a single-author research project right now, but the workflow below
is set up so it scales if anyone else joins in, and so the version
history stays clean regardless.

## Branching

- `main` — always deployable, always green CI. Protected.
- `develop` — integration branch. Merges from feature branches land here.
- `feature/<short-name>` — all new work. Branched from `develop`.
- `fix/<short-name>` — bugfixes. Branched from `develop` (or `main` for hotfixes).
- `strategy/<name>` — new strategy work. Isolated so it can be abandoned cleanly.

Release flow: `develop` -> PR to `main` -> tag `vX.Y.Z` -> release workflow fires.

## Commits

Conventional-commit flavored, not strict:

```
<type>: <short imperative summary>

<optional body>
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `strategy`, `risk`.

Good:
```
feat: add sum-of-probabilities arb strategy
fix: correct Kalshi base URL for April 2026 migration
strategy: rework cross-market arb to one-legged mode
risk: tighten daily loss limit default to $50
```

Bad (don't):
```
updates
stuff
wip
```

## Before pushing

```bash
# Format + lint + test
ruff check pm_bot run_bot.py scripts tests
black pm_bot run_bot.py scripts tests
pytest tests/ -v
```

CI will run these on push. Failing lint or tests will block merge to `main`.

## Secrets

Never commit `.env`, `.pem`, `data/`, or `logs/`. `.gitignore` handles this
but double-check every diff. If you leak a Kalshi API key, rotate it
immediately in Kalshi settings.

## Tests

- Every strategy change needs at least one unit test.
- Every risk-manager change needs a test covering the rule it touches.
- Math utilities have near-full coverage; keep it that way.
- Don't write tests that hit live exchanges. Mock adapters instead.

## Paper trade before live

If you're changing strategy or risk behavior, paper-trade at least one
full day before flipping `paper_trading: false`. The `--dry` flag doesn't
count (it skips order placement entirely, so you can't validate fills).

## Reviewing PRs

- Is there a test?
- Does it affect live trading behavior? If so, flag for extra scrutiny.
- Does it change any risk default? If so, require explicit sign-off.
- Any secrets, keys, or PII in the diff? Block.
