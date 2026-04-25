# Findings — what we discovered while you were in the shower

## The big bug we fixed

The Kalshi adapter was reading the OLD pre-March-2026 API field names:
- `yes_bid` / `yes_ask` → renamed to `yes_bid_dollars` / `yes_ask_dollars`
- `volume` → renamed to `volume_fp`
- Orderbook response: `orderbook` → `orderbook_fp`, with `yes`/`no` keys → `yes_dollars`/`no_dollars`
- All prices changed from integer cents to decimal-string dollars

**This is why every market we looked at had volume=0 and dead orderbooks.** The parser was silently returning empty Markets for the entire prod environment.

After the patch in `pm_bot/exchanges/kalshi.py`, prod markets show real data:
- KXFEDDECISION-28JAN: 5 mutually-exclusive legs, vol 1,800+ each
- KXNBAWEST-26: 8 legs, $1-3M volume per leg
- KXNBA-26 (NBA Champion): 16 legs, full orderbooks
- KXCPI-26NOV: 7 legs (cumulative thresholds)

## What's actually tradeable

### Kalshi has active orderbooks on:
| Series | Type | Notes |
|--------|------|-------|
| KXNBAGAME-* | Per-game NBA winners | Vol 5-10k per game |
| KXNBAEAST-26, KXNBAWEST-26 | Conference champions | Vol 1-3M per leg |
| KXNBA-26 | NBA Champion 2026 | Vol 1-3M per leg, 16 legs |
| KXFEDDECISION-* | Fed decisions per meeting | Vol 1k-2k per leg |
| KXCPI-* | Monthly CPI thresholds | Vol 700-2000 per leg, cumulative |

### Kalshi has DEAD books on:
- KXPRESNOMD-28 (2028 Dem Nominee) — 45 markets, 0 with books
- KXNEWPOPE-70, KXXISUCCESSOR, KXNEXTISRAELPM, etc. — long-tail political
- BTC/ETH price strikes (KXBTC-26MAY01 etc.) — too far out, no MM activity

## Cross-exchange edges found (April 24, 2026 snapshot)

### NBA Western Conference Champion 2026
| Team | Polymarket bid/ask | Kalshi bid/ask | Edge |
|------|---|---|---|
| **San Antonio Spurs** | 0.163/0.167 | 0.180/0.190 | **1.3¢** P_YES+K_NO |
| Oklahoma City | 0.640/0.650 | 0.650/0.660 | 0¢ (parity) |
| Denver | 0.067/0.070 | 0.060/0.080 | 0¢ |
| LA Lakers | 0.061/0.062 | 0.050/0.060 | 0.1¢ |

### NBA Champion 2026
| Team | Polymarket | Kalshi | Edge |
|------|---|---|---|
| **Boston Celtics** | 0.116/0.117 | 0.130/0.140 | **1.3¢** P_YES+K_NO |
| **Oklahoma City** | 0.510/0.520 | 0.530/0.540 | **1.0¢** P_YES+K_NO |
| San Antonio | 0.123/0.126 | 0.120/0.130 | 0¢ |

### NBA Eastern Conference Champion 2026
| Team | Polymarket | Kalshi | Edge |
|------|---|---|---|
| Boston | 0.412/0.418 | 0.430/0.440 | 1.2¢ |

## Math on the SAS arb (the most persistent one)

Strategy: BUY YES on Polymarket at 0.167, BUY NO on Kalshi at 0.82 (= 1 - K_bid 0.18)

Cost: 0.167 + 0.82 = **$0.987 per contract pair**
Payoff: $1.00 either way (one side wins, one loses, exactly one of YES/NO resolves true)
Gross profit: **1.3¢ per contract pair**

Fees:
- Polymarket fee at 0.167: ~0.5¢
- Kalshi taker fee at 0.82: 0.07 × 0.82 × 0.18 = 1.03¢
- **Net on taker fills: -0.2¢ (small loss)**

If Kalshi side is filled as MAKER (post limit, wait for take):
- Kalshi maker fee: 0.07 × 0.82 × 0.18 × 0.25 = 0.26¢
- **Net on maker fill: +0.5¢ profit per contract pair**

At 100 contracts × 5 trades per day × 0.5¢ = $2.50/day = ~$900/year on $1k bankroll.
Realistic with execution slippage and missed maker fills: $200-500/year on $1k = 20-50% annualized.

That's a real edge. Not life-changing, but real.

## What we built today

| Component | Path | Status |
|-----------|------|--------|
| Kalshi parser fix (new field names) | `pm_bot/exchanges/kalshi.py` | Done |
| Kalshi orderbook parser fix | `pm_bot/exchanges/kalshi.py` | Done |
| Cross-exchange event map | `config/event_map.yaml` | 3 NBA pairs, verified |
| Live scanner with SQLite logging | `scripts/cross_exchange_scanner.py` | Working |
| Diagnostic tools | `scripts/scan_diagnostic.py` (Kalshi), `scripts/polymarket_diagnostic.py` (Poly) | Working |

## Next steps when you're back

1. **Run the scanner continuously** (do this in a long-lived PowerShell window):
   ```powershell
   python scripts\cross_exchange_scanner.py --threshold 0.5 --interval 120
   ```
   This will sweep all 3 pairs every 2 minutes, log everything to `data/cross_scan.db`.
   Let it run for an hour, then check the candidates table.

2. **Get Polymarket API keys** — needed to actually execute. Run:
   ```powershell
   python scripts\polymarket_setup_api_keys.py
   ```
   (after adding your wallet's POLYMARKET_PRIVATE_KEY to .env)

3. **Wire the strategy into pm_bot** for live trading. The scanner is research-mode;
   the actual `cross_market_arb` strategy in `pm_bot/strategies/` needs to be patched
   to use the same logic for paper trading and eventually live.

4. **Add more event pairs** to `config/event_map.yaml` as you find them. Boston in
   the NBA Champion event is showing 1.3¢ persistently — worth adding more
   verification.

5. **Look at maker-fill rates.** The whole math depends on getting the Kalshi side
   filled as a maker. Need to test how often a passive limit order at K_bid actually
   gets filled before the price moves. This is the key empirical question.

## Honest read

We found 1-1.3¢ gross gaps consistently on liquid NBA cross-exchange comparisons.
After fees, this is roughly break-even on taker fills, ~0.5¢ profitable on maker
fills. Real edge but tight. Volume on these markets is high enough ($1-3M per leg
on Kalshi) that the bot can place size without moving the market.

The bigger gaps (5-10¢) live in less-liquid markets where Kalshi has dead books,
which means we can't actually execute. The "easy money" arb that the literature
documents is mostly already-arb'd-away by funded bots on the liquid side and
not-tradeable on the illiquid side.

Realistic returns: 20-50% annualized on a $1k bankroll, IF execution works as
expected. Worth running. Worth respecting that this is not get-rich-quick.
