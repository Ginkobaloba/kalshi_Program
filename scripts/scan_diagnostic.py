"""
Diagnostic v4: sample-based scan that respects basic-tier rate limits.

Kalshi prod has 3401+ mutually-exclusive events. Scanning all of them
takes hours at 20 reads/sec. This version SAMPLES intelligently:
  - Only first 1000 events (paginated) — covers active events
  - Filters to mutually-exclusive
  - Sorts by 'last_updated_ts' descending (most-active first)
  - Caps at 50 events for orderbook scan
  - Throttles to 1 req/sec — slow but lint-clean

Usage:
  python scripts/scan_diagnostic.py             # demo env
  python scripts/scan_diagnostic.py prod        # prod env
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pm_bot.exchanges.kalshi import KalshiAdapter

EVENT_PAGES = 5         # 5 pages * 200 = 1000 events sampled
EVENT_SCAN_CAP = 50     # cap orderbook scans at this many events
THROTTLE_SEC = 1.0      # 1 req/sec — very polite, no 429s


def fetch_events_page(k: KalshiAdapter, cursor: str | None) -> tuple[list[dict], str | None]:
    params = {"limit": 200, "status": "open"}
    if cursor:
        params["cursor"] = cursor
    data = k._request("GET", "/events", params=params)
    return data.get("events", []) or [], data.get("cursor")


def main() -> int:
    env = sys.argv[1] if len(sys.argv) > 1 else "demo"
    print(f"=== Kalshi {env.upper()} env (sampled scan, throttled) ===\n")

    k = KalshiAdapter(env=env)

    print(f"[1/4] Sampling first {EVENT_PAGES * 200} events...")
    events: list[dict] = []
    cursor: str | None = None
    for page_idx in range(EVENT_PAGES):
        time.sleep(THROTTLE_SEC)
        try:
            page, cursor = fetch_events_page(k, cursor)
        except Exception as e:
            print(f"  page {page_idx + 1} error: {e}")
            break
        events.extend(page)
        print(f"      page {page_idx + 1}: +{len(page)} events  total={len(events)}")
        if not cursor:
            break

    me_events = [e for e in events if e.get("mutually_exclusive")]
    print(f"\n      mutually-exclusive in sample: {len(me_events)}")

    # Sort by recency (most recently updated first)
    me_events.sort(
        key=lambda e: e.get("last_updated_ts") or "",
        reverse=True,
    )
    me_events = me_events[:EVENT_SCAN_CAP]
    print(f"      scanning top {len(me_events)} by recency\n")

    print("[2/4] Fetching market lists per event (1 req/sec)...")
    candidates: list[tuple[dict, list]] = []
    for ev in me_events:
        ticker = ev.get("event_ticker") or ev.get("ticker")
        if not ticker:
            continue
        time.sleep(THROTTLE_SEC)
        try:
            markets = k.list_markets(event_id=ticker, limit=100)
        except Exception:
            continue
        if len(markets) >= 3:
            candidates.append((ev, markets))
    print(f"      events with 3+ markets: {len(candidates)}\n")

    if not candidates:
        print("No multi-leg events found in sample.")
        return 0

    print("[3/4] Pulling orderbooks for each leg (slowest step, ~1 req/sec)...")
    total_books = sum(len(m) for _, m in candidates)
    print(f"      will pull ~{total_books} orderbooks ({total_books * THROTTLE_SEC:.0f}s estimated)\n")

    print(f"{'sum(ASK)':>9} {'sum(BID)':>9} {'covg':>7}  {'event':<28}  title")
    print("-" * 110)

    arb_candidates = []
    for idx, (ev, markets) in enumerate(candidates, 1):
        ticker = ev.get("event_ticker", "?")
        title = ev.get("title", "")[:45]

        yes_asks: list[float] = []
        yes_bids: list[float] = []

        for m in markets:
            time.sleep(THROTTLE_SEC)
            try:
                book = k.get_orderbook(m.ticker, depth=1)
            except Exception:
                continue
            if book is None:
                continue
            best_ask = book.best_yes_ask()
            best_bid = book.best_yes_bid()
            if best_ask:
                yes_asks.append(best_ask.price)
            if best_bid:
                yes_bids.append(best_bid.price)

        sum_ask = sum(yes_asks)
        sum_bid = sum(yes_bids)
        n_legs = len(markets)
        covg = f"{len(yes_asks)}/{n_legs}"

        marker = ""
        if len(yes_asks) == n_legs and sum_ask > 1.03:
            marker = "  *** ARB"
            arb_candidates.append((ticker, sum_ask, n_legs, title))
        elif len(yes_asks) == n_legs and sum_ask > 1.0:
            marker = "  marginal"

        print(f"  {sum_ask:>7.3f}  {sum_bid:>7.3f}  {covg:>7}  {ticker:<28}  {title}{marker}")

    print()
    print("[4/4] Summary")
    if arb_candidates:
        print(f"\n=== {len(arb_candidates)} ARB CANDIDATE(S) (sum > 1.03, full coverage) ===")
        for ticker, total, n, title in arb_candidates:
            edge_per_basket = (total - 1) * 100
            print(f"  ${edge_per_basket:5.2f} per 100-basket  sum(YES)={total:.3f}  "
                  f"{n} legs  {ticker}  ({title})")
    else:
        print("\nNo arb candidates with sum > 1.03 in this sample.")
        print("Try re-running — the sort by recency rotates which events get scanned.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
