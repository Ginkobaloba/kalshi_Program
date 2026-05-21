# kalshi_Program

Algorithmic trading harness for prediction markets (Kalshi and Polymarket), built around a paper-trade-first design philosophy and a $1k pilot bankroll. Python.

This repo contains two related pieces:

- **`kalshi_edge_finder.py`** -- the original standalone signal scanner. Still runs on its own and is treated as a signal source by the newer harness.
- **`pm_bot/`** (v0.2.0) -- the modular trading bot, with exchange adapters, strategy modules, risk management, and persistence. See [`README_PM_BOT.md`](./README_PM_BOT.md) for the full architecture and operating notes.

## Design principles

- **Paper trading is the default.** Live execution requires an explicit flip in `config.yaml`.
- **Conservative position sizing.** Fractional Kelly (0.25 default), per-contract caps, daily-loss kill switch.
- **Compliance-aware adapters.** Polymarket International is read-only for reference pricing; Polymarket US (QCX) is stubbed and disabled pending access.
- **Edges are measured, not assumed.** The harness exists to verify whether a strategy has any edge on this setup before any live capital is committed.

## Strategy modules

- **Sum-of-probabilities arbitrage.** Exploits mutually-exclusive contract sets whose implied probabilities sum materially above or below $1.
- **Cross-market arbitrage.** Tracks Kalshi vs Polymarket pricing on equivalent events; one-legged directional mode available.
- **Market making (stub).** Maker-taker fee gap on Kalshi as a structural edge.
- **News / signal reaction (stub).** Plugin-style harness that consumes external signal sources (FRED, NOAA, scores).

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env
# set FRED_API_KEY (and other secrets) in .env
python scripts/test_kalshi_auth.py
python run_bot.py
```

When ready for live trading, set `paper_trading: false` in `config.yaml` and start with `max_position_usd` of $10 or less.

## Honest framing

Documented edges on US prediction markets are real but small, fast-decaying, and contested by funded bots. This is a research and pilot harness, not a money printer. The point of the repo is to measure edges rigorously, automate execution carefully, and keep capital protected by default.

## License

MIT.
