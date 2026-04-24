# Prediction Market Bot (`pm_bot`)

A modular, paper-trade-first algorithmic trader for Kalshi and (eventually) Polymarket. Built for a pilot bankroll around $1k.

This is v2 of the project. Your original `kalshi_edge_finder.py` still works on its own and is treated here as a signal source.

## What this actually is

A research / pilot harness, not a money printer. The documented edges in prediction markets are real but small, fast-decaying, and already being hunted by funded bots. This system is built so you can (a) measure whether a strategy has any edge on your setup before putting real money on it, and (b) automate execution once you've verified it does.

**Defaults are conservative on purpose:**
- Paper trading is ON by default. You have to explicitly flip it OFF in `config.yaml`.
- Polymarket International trading is OFF and flagged with a ToS/compliance warning.
- Polymarket US (QCX) adapter is stubbed and disabled until you have an invite.
- Daily loss limit kicks the kill switch automatically.
- Position sizing uses fractional Kelly (0.25) by default, not full Kelly.

## Architecture

```
run_bot.py                   # entry point, orchestrator
pm_bot/
  config.py                  # YAML + env loading
  models.py                  # Market, OrderBook, Order, Fill, Position (pydantic)
  logger.py                  # structured logs to file + console
  exchanges/
    base.py                  # ExchangeAdapter abstract base
    kalshi.py                # Kalshi REST (RSA-PSS) + WS
    polymarket.py            # International CLOB, read-only for reference pricing
    polymarket_us.py         # QCX stub, feature-flagged OFF
    paper.py                 # paper-trading wrapper around any adapter
  strategies/
    base.py                  # Strategy abstract base
    sum_prob_arb.py          # multi-outcome implied-probability-sum arb
    cross_market_arb.py      # Kalshi <-> Polymarket price divergence
    market_maker.py          # thin-book two-sided quoting (stub, disabled)
    news_signal.py           # signal-driven directional bot (stub)
  signals/
    external.py              # wraps your existing FRED/NOAA clients
  risk/
    manager.py               # fractional Kelly, per-contract caps, kill switch
  persistence/
    db.py                    # SQLite trade/fill/pnl log
  util/
    signing.py               # Kalshi RSA-PSS request signing
    math.py                  # Kelly, probability conversions, fee calcs
```

## Quick start

```bash
# 1. Install
pip install -r requirements.txt

# 2. Set secrets
cp .env.example .env
# edit .env, at minimum set FRED_API_KEY

# 3. Generate your Kalshi API key
# Log into Kalshi -> Settings -> API Keys -> Create
# Download the private key file, save as kalshi_private.pem
# Put the key ID in .env as KALSHI_API_KEY_ID

# 4. Smoke test the auth
python scripts/test_kalshi_auth.py

# 5. Run in paper trading mode (default)
python run_bot.py

# 6. When ready, in config.yaml set paper_trading: false
#    START SMALL. Set max_position_usd to $10 or less first.
```

## Strategies

### Sum-of-probabilities arbitrage (`sum_prob_arb`)
Mutually-exclusive multi-outcome contracts should have implied probabilities summing to exactly $1. They often don't. If the sum is materially > 1 (after fees), you short the basket — buy NO on every outcome. One outcome resolves YES ($0 for that leg), the rest resolve NO ($1 each), net profit = (N-1) - sum(NO prices) - fees.

Published: 1.5-3% per trade on Polymarket equivalents; opportunities are thinner on Kalshi but real on event-contract markets with many mutually-exclusive outcomes.

### Cross-market arbitrage (`cross_market_arb`)
Same event, different price on Kalshi vs Polymarket. Example from Feb 2026: YES at 58¢ on Kalshi, NO at 35¢ on Polymarket → $0.93 to guarantee $1 payout → 7.5% gross. Typical windows: 2-7 seconds. Risks: settlement criteria divergence (Kalshi says YES, Poly says NO — you lose on both legs), fee drag, execution slippage.

**Current state:** Polymarket International leg is DISABLED. We track prices for research. One-legged mode available — trade only the Kalshi side when Poly price implies Kalshi is mispriced (directional bet, not arb).

### Market making (`market_maker`, stub)
Post tight two-sided quotes in thin Kalshi books, capture the spread. Kalshi's maker fee is 25% of the taker fee → at $0.50 mid, maker pays 0.44¢ vs taker's 1.75¢. That 1.3¢ gap per round-trip is the structural edge (per Becker 2025 microstructure paper — makers systematically beat takers on Kalshi, not from forecasting, from flow capture).

**Current state:** Stubbed and disabled. Requires meaningful inventory; $1k bankroll can market-make maybe 1-2 contracts worth of depth which is too small to matter.

### News/signal reaction (`news_signal`, stub)
Plugin-style. A signal source (NOAA forecast, FRED print, sports score) emits a probability estimate; if it differs from the market by more than the threshold, submit a directional order.

**Current state:** Stub with one example (NOAA weather). Heavily gated — weather markets are mostly arb'd out per Northlake Labs' public postmortem. Don't expect returns from this without a novel signal source.

## Risk management

Defaults for a $1k pilot bankroll:

- `kelly_fraction: 0.25` — fractional Kelly to keep variance reasonable
- `max_position_usd: 50` — never more than $50 on a single contract
- `max_concurrent_positions: 5` — diversification floor
- `daily_loss_limit_usd: 50` — 5% daily drawdown hits the kill switch
- `min_edge_bps: 200` — need 2% after-fee edge to open a position
- `min_book_depth_contracts: 20` — don't trade 10-contract books

Edit these in `config.yaml`. Don't raise them without reason.

## Compliance notes

**Polymarket International** (polymarket.com) geoblocks US users from trading. This program does NOT send orders to the international CLOB from US IPs, and the `polymarket` adapter raises `ComplianceError` if you try to enable trading on it. VPN workarounds violate Polymarket's ToS and sit in a legal gray area — don't go there with this codebase.

**Polymarket US / QCX** — as of April 2026, Polymarket acquired QCEX and relaunched a CFTC-regulated US product. Currently invite-only, estimated full public launch Q3-Q4 2026. The `polymarket_us` adapter is ready to flip on when you have access.

**Kalshi** is CFTC-regulated and fully open to US adults with an account. State-level lawsuits over sports contracts are pending (NV, NJ, MD, OH, MT, IL) but do not currently prevent access.

## Honest risk disclosure

- You will probably lose money on this before you make money on it. Budget for tuition.
- Fees eat 3-5% of contract value on a $0.50 round-trip. Your edge has to beat that.
- Prediction markets have skewed, fat-tailed distributions. A $1k bankroll can absolutely hit a bad streak that looks like the strategy is broken when it isn't, or vice versa.
- Quant firms (Susquehanna, Citadel Securities, others) market-make on Kalshi's liquid contracts. The long-tail/weekly/illiquid markets are where retail can still find edge. That's where this bot looks.
- Nothing here is financial advice. It's a research tool.

## TODO / known gaps

- WebSocket feed integration for Kalshi (currently REST polling)
- Polymarket US full implementation (blocked on invite)
- Backtester with historical data (needs storage of order book snapshots)
- Prometheus metrics endpoint for monitoring
- Real fill simulation in paper mode (currently optimistic — assumes your limit price hit)
