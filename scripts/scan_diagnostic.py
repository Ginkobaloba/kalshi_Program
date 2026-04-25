"""
Improved diagnostic: paginate through Kalshi to find liquid multi-outcome events.

Approach:
  1. Hit /events endpoint directly — Kalshi groups markets server-side
  2. For events with 3+ open markets, fetch the constituent markets
  3. Compute the YES-price sum (the sum-prob-arb signal)

Usage:
  python scripts/scan_diagnostic.py             # demo env
  python scripts/scan_diagnostic.py prod        # prod env
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pm_bot.exchanges.kalshi import KalshiAdapter


def fetch_all_events(k: KalshiAdapter, max_pages: int = 20) -> list[dict]:
    """Paginate through /events to collect all open events."""
    events: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        params = {"limit": 200, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        data = k._request("GET", "/events", params=params)
        page = data.get("events", []) or []
        if not page:
            break
        events.extend(page)
        cursor = data.get("cursor")
        if not cursor:
            break
    return events


def main() -> int:
    env = sys.argv[1] if len(sys.argv) > 1 else "demo"
    print(f"=== Kalshi {env.upper()} env ===\n")

    k = KalshiAdapter(env=env)

    print("Fetching all open events (paginated)...")
    events = fetch_all_events(k)
    print(f"  total open events:      {len(events)}\n")

    if not events:
        print("No events returned. Check connectivity.")
        return 1

    # Each event has a `markets` count (or we can re-query)
    # Many event objects include market_count or similar — let's see.
    sample = events[0]
    print("Sample event keys:", list(sample.keys())[:15])
    print()

    # Try to get markets count from event object
    multi_outcome: list[tuple[str, int]] = []
    for ev in events:
        ticker = ev.get("event_ticker") or ev.get("ticker") or ev.get("series_ticker", "?")
        # Different field names depending on response shape
        n_markets = ev.get("market_count") or ev.get("markets_count")
        if n_markets is None and "markets" in ev:
            n_markets = len(ev["markets"]) if isinstance(ev["markets"], list) else None
        if n_markets and n_markets >= 3:
            multi_outcome.append((ticker, n_markets, ev.get("title", "")[:60]))

    print(f"Events with >=3 markets (from event metadata): {len(multi_outcome)}")
    for tup in multi_outcome[:15]:
        ticker, n, title = tup
        print(f"  {n:3d} legs  {ticker:30s}  {title}")

    if not multi_outcome:
        print("\nFalling back: querying markets per event manually...")
        # For each event, fetch its markets
        keepers = []
        for ev in events[:50]:  # cap to avoid hammering
            ticker = ev.get("event_ticker") or ev.get("ticker")
            if not ticker:
                continue
            try:
                markets = k.list_markets(event_id=ticker, limit=20)
                if len(markets) >= 3:
                    keepers.append((ticker, markets, ev.get("title", "")[:60]))
            except Exception:
                continue

        print(f"  Found {len(keepers)} events with 3+ markets")
        for ticker, markets, title in keepers[:10]:
            valid = [m for m in markets if 0 < m.yes_ask < 1]
            if not valid:
                print(f"  {ticker:30s}  {len(markets)} markets, all empty books  ({title})")
                continue
            ys = sum(m.yes_ask for m in valid)
            avg_vol = sum(m.volume for m in valid) / len(valid)
            print(f"  sum(YES)={ys:.3f}  legs={len(valid):2d}  avg_vol={avg_vol:5.0f}  "
                  f"{ticker}  ({title})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
